"""OpenBrain (semantic memory) router.

The UI calls these to capture thoughts and search across them.

Endpoints:

    POST /openbrain/capture          capture a piece of content with provenance
    POST /openbrain/search           semantic search
    GET  /openbrain/recent           most-recent thoughts
    GET  /openbrain/stats            counts + by-source aggregates
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from agent_core.openbrain import OpenBrainStore
from agent_core.web.auth import require_token

router = APIRouter(
    prefix="/openbrain",
    tags=["openbrain"],
    dependencies=[Depends(require_token)],
)


# ── Schemas ────────────────────────────────────────────────────────────────


class CaptureBody(BaseModel):
    content: str = Field(min_length=1)
    source_kind: str | None = Field(
        default=None,
        description="vault|gmail|drive|github|notion|slack|linear|bookmarks|downloads|calendar|other",
    )
    source_uri: str | None = None
    source_title: str | None = None
    metadata: dict[str, Any] | None = None


class CaptureOut(BaseModel):
    id: str
    fingerprint: str
    was_existing: bool


class SearchBody(BaseModel):
    query: str = Field(min_length=1)
    limit: int | None = Field(default=None, ge=1, le=100)
    threshold: float | None = Field(default=None, ge=-1.0, le=1.0)
    source_filter: str | None = None


class SourceOut(BaseModel):
    source_kind: str
    source_uri: str | None
    source_title: str | None
    fetched_at: datetime | None


class HitOut(BaseModel):
    id: str
    content: str
    similarity: float
    sources: list[SourceOut]


class ThoughtOut(BaseModel):
    id: str
    content: str
    fingerprint: str | None
    created_at: datetime | None


# ── Routes ─────────────────────────────────────────────────────────────────


@router.post("/capture", response_model=CaptureOut)
def capture(request: Request, body: CaptureBody) -> CaptureOut:
    """Persist a thought (idempotent on content fingerprint)."""
    store: OpenBrainStore = request.app.state.openbrain
    # Detect existence pre-capture so the response can tell the UI whether
    # this was a new thought or a dedup hit (UX surfaces this differently).
    from sqlmodel import select

    from agent_core.openbrain.store import _fingerprint  # type: ignore[attr-defined]
    from agent_core.state.models import Thought

    fp = _fingerprint(body.content)
    with store.db.session() as s:
        existed = s.exec(select(Thought).where(Thought.fingerprint == fp)).first() is not None

    thought = store.capture(
        body.content,
        metadata=body.metadata,
        source_kind=body.source_kind,
        source_uri=body.source_uri,
        source_title=body.source_title,
    )
    return CaptureOut(id=thought.id, fingerprint=thought.fingerprint or "", was_existing=existed)


@router.post("/search", response_model=list[HitOut])
def search(request: Request, body: SearchBody) -> list[HitOut]:
    store: OpenBrainStore = request.app.state.openbrain
    hits = store.search(
        body.query,
        limit=body.limit,
        threshold=body.threshold,
        source_filter=body.source_filter,
    )
    return [
        HitOut(
            id=h.thought.id,
            content=h.thought.content,
            similarity=round(h.similarity, 4),
            sources=[
                SourceOut(
                    source_kind=src.source_kind,
                    source_uri=src.source_uri,
                    source_title=src.source_title,
                    fetched_at=src.fetched_at,
                )
                for src in h.sources
            ],
        )
        for h in hits
    ]


@router.get("/recent", response_model=list[ThoughtOut])
def recent(request: Request, limit: int = 10, source_filter: str | None = None):
    store: OpenBrainStore = request.app.state.openbrain
    rows = store.recent(limit=limit, source_filter=source_filter)
    return [
        ThoughtOut(
            id=t.id,
            content=t.content,
            fingerprint=t.fingerprint,
            created_at=t.created_at,
        )
        for t in rows
    ]


@router.get("/stats")
def stats(request: Request) -> dict[str, Any]:
    store: OpenBrainStore = request.app.state.openbrain
    return store.stats()
