"""OpenBrainStore — capture + search semantic memory.

Wraps the Thought table with the operations skills actually need:

  capture(content, metadata, source)
    → embed via the EmbeddingProvider, persist Thought row + ThoughtSource
    → idempotent on content fingerprint (sha256 of normalized content)

  search(query, limit, threshold, source_filter)
    → embed query, cosine-similarity against all stored embeddings,
      return top-K above threshold, with source provenance attached

  recent(limit, source_filter)
    → most-recent thoughts, optionally filtered by source

Vector storage: JSON list of floats on the Thought row. Cosine sim in
Python at query time. Fast enough for ~tens of thousands of thoughts;
native vector backends (pgvector / sqlite-vec) come in a future sprint
when the scale demands.

Provenance: each captured thought writes a ThoughtSource row recording
where it came from (source_kind: vault / gmail / drive / etc.; source_uri,
title, freshness, authority, visibility). Per L16's "source attribution
on every answer."
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Any

from sqlmodel import select

from agent_core.openbrain.embeddings import EmbeddingProvider
from agent_core.state.db import Database
from agent_core.state.models import Thought, ThoughtSource, utcnow

logger = logging.getLogger(__name__)


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class SearchHit:
    """One result from OpenBrainStore.search()."""

    thought: Thought
    similarity: float  # cosine; 1.0 = identical, -1.0 = opposite
    sources: list[ThoughtSource]


# ── Store ────────────────────────────────────────────────────────────────────


class OpenBrainStore:
    """Capture + search semantic memory.

    Args:
        db: agent-core Database
        embeddings: EmbeddingProvider (production: OllamaEmbeddingProvider;
                                       tests: StubEmbeddingProvider)
        search_default_limit: Default top-K for ``search()`` when caller
            doesn't pass ``limit``. Wired from settings so the user can
            tune retrieval breadth without code changes.
        search_default_threshold: Default cosine floor for ``search()``.
            Wired from settings.

    Prefer ``OpenBrainStore.from_settings(settings, db)`` — it picks the
    right embedding provider and propagates search defaults. The bare
    constructor stays for tests and advanced wiring.
    """

    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingProvider,
        *,
        search_default_limit: int = 5,
        search_default_threshold: float = 0.0,
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.search_default_limit = search_default_limit
        self.search_default_threshold = search_default_threshold

    # ── Factory ─────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: Any, db: Database) -> OpenBrainStore:
        """Build an OpenBrainStore from an ``AgentSettings`` instance.

        Reads ``settings.openbrain.*`` and instantiates the right embedding
        provider. Use this from skill/app code; it keeps wiring out of the
        callers and makes "user changed embedding model in agent.yml"
        actually work.

        Type annotation is ``Any`` to keep this module decoupled from
        ``agent_core.settings`` at import time (avoids circular imports
        when settings ever wants to reach into the store).
        """
        # Local import to keep module-level import graph free of settings.
        from agent_core.openbrain.embeddings import (
            OllamaEmbeddingProvider,
            SemanticStubProvider,
            StubEmbeddingProvider,
        )

        ob = settings.openbrain
        provider: EmbeddingProvider
        if ob.embedding_provider == "ollama":
            provider = OllamaEmbeddingProvider(
                base_url=ob.ollama_base_url,
                model=ob.embedding_model,
            )
        elif ob.embedding_provider == "stub":
            provider = StubEmbeddingProvider()
        elif ob.embedding_provider == "stub-semantic":
            provider = SemanticStubProvider()
        else:
            raise ValueError(
                f"unknown openbrain.embedding_provider: {ob.embedding_provider!r}"
            )
        return cls(
            db,
            provider,
            search_default_limit=ob.search_default_limit,
            search_default_threshold=ob.search_default_threshold,
        )

    # ── Capture ─────────────────────────────────────────────────────────────

    def capture(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        source_kind: str | None = None,
        source_uri: str | None = None,
        source_title: str | None = None,
        authority: str | None = None,
        visibility: str = "all",
        valid_from: Any = None,
        valid_until: Any = None,
    ) -> Thought:
        """Persist a thought (idempotent on content fingerprint).

        If a thought with the same fingerprint already exists, the existing
        row is returned and a *new* ThoughtSource row is appended (so we
        track every place a piece of content was seen).

        Re-embedding only happens for genuinely new content.
        """
        fingerprint = _fingerprint(content)

        with self.db.session() as s:
            existing = s.exec(select(Thought).where(Thought.fingerprint == fingerprint)).first()
            if existing is not None:
                if source_kind is not None:
                    s.add(
                        ThoughtSource(
                            thought_id=existing.id,
                            source_kind=source_kind,
                            source_uri=source_uri,
                            source_title=source_title,
                            valid_from=valid_from,
                            valid_until=valid_until,
                            authority=authority,
                            visibility=visibility,
                        )
                    )
                    s.commit()
                return existing

        # New thought — embed + persist
        vec = self.embeddings.embed(content)
        thought = Thought(
            content=content,
            fingerprint=fingerprint,
            metadata_json=metadata,
            embedding=vec,
            embedding_model=self.embeddings.model_id,
        )
        with self.db.session() as s:
            s.add(thought)
            if source_kind is not None:
                s.add(
                    ThoughtSource(
                        thought_id=thought.id,
                        source_kind=source_kind,
                        source_uri=source_uri,
                        source_title=source_title,
                        valid_from=valid_from,
                        valid_until=valid_until,
                        authority=authority,
                        visibility=visibility,
                    )
                )
            s.commit()
            s.refresh(thought)
        logger.info("captured thought: id=%s fp=%s", thought.id[:8], fingerprint[:8])
        return thought

    def reindex(self, thought_id: str) -> Thought:
        """Re-embed an existing thought with the current EmbeddingProvider.

        Use after switching embedding models (the model_id changes; old
        vectors are no longer in the same space)."""
        with self.db.session() as s:
            thought = s.get(Thought, thought_id)
            if thought is None:
                raise ValueError(f"thought {thought_id!r} not found")
            content = thought.content

        vec = self.embeddings.embed(content)
        with self.db.session() as s:
            thought = s.get(Thought, thought_id)
            thought.embedding = vec
            thought.embedding_model = self.embeddings.model_id
            thought.updated_at = utcnow()
            s.add(thought)
            s.commit()
            s.refresh(thought)
        return thought

    # ── Search ─────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        threshold: float | None = None,
        source_filter: str | None = None,
    ) -> list[SearchHit]:
        """Cosine-similarity search over stored embeddings.

        Args:
            query: text to embed and compare against
            limit: max hits returned. None → ``search_default_limit``
                from settings (default 5).
            threshold: cosine ≥ this. None → ``search_default_threshold``
                from settings (default 0.0).
            source_filter: only include thoughts that have a ThoughtSource
                           with source_kind matching this string

        Skips thoughts indexed with a different embedding_model (different
        vector space; comparisons would be meaningless).
        """
        if limit is None:
            limit = self.search_default_limit
        if threshold is None:
            threshold = self.search_default_threshold
        query_vec = self.embeddings.embed(query)

        with self.db.session() as s:
            stmt = select(Thought).where(Thought.embedding.is_not(None))
            stmt = stmt.where(Thought.embedding_model == self.embeddings.model_id)
            thoughts = list(s.exec(stmt).all())

            sources_by_thought: dict[str, list[ThoughtSource]] = {}
            if source_filter:
                # Pre-filter to thoughts that have at least one matching source
                matching_thought_ids = {
                    src.thought_id
                    for src in s.exec(
                        select(ThoughtSource).where(ThoughtSource.source_kind == source_filter)
                    ).all()
                }
                thoughts = [t for t in thoughts if t.id in matching_thought_ids]

            # Always load all sources for the surviving thoughts so SearchHit
            # can carry provenance.
            for src in s.exec(select(ThoughtSource)).all():
                sources_by_thought.setdefault(src.thought_id, []).append(src)

        scored: list[tuple[float, Thought]] = []
        for t in thoughts:
            sim = _cosine(query_vec, t.embedding or [])
            if sim >= threshold:
                scored.append((sim, t))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        scored = scored[:limit]

        return [
            SearchHit(
                thought=t,
                similarity=sim,
                sources=sources_by_thought.get(t.id, []),
            )
            for sim, t in scored
        ]

    # ── Recent ─────────────────────────────────────────────────────────────

    def recent(
        self,
        *,
        limit: int = 10,
        source_filter: str | None = None,
    ) -> list[Thought]:
        """Most-recent thoughts, optionally filtered by source kind."""
        with self.db.session() as s:
            stmt = select(Thought).order_by(Thought.created_at.desc())
            thoughts = list(s.exec(stmt).all())
            if source_filter:
                ids = {
                    src.thought_id
                    for src in s.exec(
                        select(ThoughtSource).where(ThoughtSource.source_kind == source_filter)
                    ).all()
                }
                thoughts = [t for t in thoughts if t.id in ids]
        return thoughts[:limit]

    # ── Stats ──────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Quick aggregates for the operational dashboard."""
        with self.db.session() as s:
            thoughts = list(s.exec(select(Thought)).all())
            sources = list(s.exec(select(ThoughtSource)).all())

        by_source: dict[str, int] = {}
        for src in sources:
            by_source[src.source_kind] = by_source.get(src.source_kind, 0) + 1

        embedded = sum(1 for t in thoughts if t.embedding)
        return {
            "thoughts": len(thoughts),
            "embedded": embedded,
            "unembedded": len(thoughts) - embedded,
            "embedding_model": self.embeddings.model_id,
            "dimensions": self.embeddings.dimensions if embedded else None,
            "sources_total": len(sources),
            "sources_by_kind": by_source,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fingerprint(content: str) -> str:
    """Stable content fingerprint for dedup. Normalizes whitespace + case."""
    normalized = " ".join(content.split()).lower().strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity on two vectors. Returns 0.0 on length mismatch or
    zero-magnitude (degenerate)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


__all__ = ["OpenBrainStore", "SearchHit"]
