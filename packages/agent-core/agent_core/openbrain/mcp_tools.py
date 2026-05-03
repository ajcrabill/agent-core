"""MCP-compatible tool functions over OpenBrain.

Mirrors Esby's openbrain_mcp.py surface (capture_thought, search_thoughts,
recent_thoughts, openbrain_stats) so existing MCP-using code keeps working
when re-wired against agent-core. Real MCP wiring lands when Hermes vendors;
these functions are usable today.
"""

from __future__ import annotations

from typing import Any

from agent_core.openbrain.store import OpenBrainStore


def capture_thought(
    store: OpenBrainStore,
    *,
    content: str,
    source_kind: str | None = None,
    source_uri: str | None = None,
    source_title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a thought; return {id, fingerprint, was_existing}."""
    # Detect dedup behavior by checking fingerprint before/after
    from sqlmodel import select

    from agent_core.openbrain.store import _fingerprint  # type: ignore[attr-defined]
    from agent_core.state.models import Thought

    fp = _fingerprint(content)
    with store.db.session() as s:
        pre_existing = s.exec(select(Thought).where(Thought.fingerprint == fp)).first()
    was_existing = pre_existing is not None

    thought = store.capture(
        content,
        metadata=metadata,
        source_kind=source_kind,
        source_uri=source_uri,
        source_title=source_title,
    )
    return {
        "id": thought.id,
        "fingerprint": thought.fingerprint,
        "was_existing": was_existing,
    }


def search_thoughts(
    store: OpenBrainStore,
    *,
    query: str,
    limit: int | None = None,
    threshold: float | None = None,
    source_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search; returns list of {id, content, similarity, sources}.

    ``limit`` / ``threshold`` default to the store's settings-driven values
    (``settings.openbrain.search_default_limit/threshold``) when omitted.
    """
    hits = store.search(query, limit=limit, threshold=threshold, source_filter=source_filter)
    return [
        {
            "id": h.thought.id,
            "content": h.thought.content,
            "similarity": round(h.similarity, 4),
            "metadata": h.thought.metadata_json,
            "sources": [
                {
                    "source_kind": src.source_kind,
                    "source_uri": src.source_uri,
                    "source_title": src.source_title,
                    "fetched_at": src.fetched_at.isoformat() if src.fetched_at else None,
                }
                for src in h.sources
            ],
        }
        for h in hits
    ]


def recent_thoughts(
    store: OpenBrainStore,
    *,
    limit: int = 10,
    source_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Most-recent thoughts, optionally filtered by source kind."""
    rows = store.recent(limit=limit, source_filter=source_filter)
    return [
        {
            "id": t.id,
            "content": t.content,
            "fingerprint": t.fingerprint,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "metadata": t.metadata_json,
        }
        for t in rows
    ]


def openbrain_stats(store: OpenBrainStore) -> dict[str, Any]:
    """Aggregates for the dashboard."""
    return store.stats()


__all__ = [
    "capture_thought",
    "openbrain_stats",
    "recent_thoughts",
    "search_thoughts",
]
