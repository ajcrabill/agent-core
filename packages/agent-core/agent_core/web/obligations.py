"""ObligationBoard router.

Surfaces the four-column board (inbox / in-progress / waiting / done)
plus the transitions between them. The OpenWebUI ObligationBoard plugin
calls these endpoints to render and mutate the board.

Endpoints:

    GET    /obligations                  list (filterable by status, owner)
    GET    /obligations/{id}             one obligation + recent events
    PATCH  /obligations/{id}             update status, owner, priority
    POST   /obligations                  create a manual obligation
    DELETE /obligations/{id}             archive (soft per L23) or hard delete

Mutation endpoints respect ``settings.autonomy.archive_instead_of_delete``:
DELETE soft-archives by default, hard-deletes only when settings allow it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlmodel import select

from agent_core.state.models import (
    Obligation,
    ObligationOwner,
    ObligationSource,
    ObligationStatus,
    utcnow,
)
from agent_core.web.auth import require_token

router = APIRouter(
    prefix="/obligations",
    tags=["obligations"],
    dependencies=[Depends(require_token)],
)


# ── Schemas ────────────────────────────────────────────────────────────────


class ObligationOut(BaseModel):
    """API-shaped Obligation. Excludes JSON-blob completion_criteria from
    list views (handlers send it on the detail endpoint)."""

    id: str
    title: str
    body: str | None
    status: ObligationStatus
    owner: ObligationOwner
    source: ObligationSource
    priority: int
    parent_id: str | None
    due_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ObligationDetail(ObligationOut):
    """Detail view adds the JSON-blob fields."""

    completion_criteria: list[dict] = Field(default_factory=list)


class ObligationCreate(BaseModel):
    """Body for ``POST /obligations``. Manual creation only — agent-spawned
    obligations come through the inbound capture pipeline."""

    title: str = Field(min_length=1, max_length=500)
    body: str | None = None
    priority: int = 0
    completion_criteria: list[dict] = Field(default_factory=list)
    due_at: datetime | None = None


class ObligationPatch(BaseModel):
    """Body for ``PATCH /obligations/{id}``. Every field optional."""

    status: ObligationStatus | None = None
    owner: ObligationOwner | None = None
    priority: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = None


# ── Routes ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ObligationOut])
def list_obligations(
    request: Request,
    status_: Annotated[ObligationStatus | None, Query(alias="status")] = None,
    owner: ObligationOwner | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ObligationOut]:
    """List obligations on the board, optionally filtered by status + owner."""
    db = request.app.state.db
    with db.session() as s:
        stmt = select(Obligation).order_by(Obligation.priority.desc(), Obligation.created_at.desc())
        if status_ is not None:
            stmt = stmt.where(Obligation.status == status_)
        if owner is not None:
            stmt = stmt.where(Obligation.owner == owner)
        rows = list(s.exec(stmt.limit(limit)).all())
    return [ObligationOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/{obligation_id}", response_model=ObligationDetail)
def get_obligation(request: Request, obligation_id: str) -> ObligationDetail:
    db = request.app.state.db
    with db.session() as s:
        ob = s.get(Obligation, obligation_id)
    if ob is None:
        raise HTTPException(status_code=404, detail=f"obligation {obligation_id!r} not found")
    return ObligationDetail.model_validate(ob, from_attributes=True)


@router.post("", response_model=ObligationDetail, status_code=status.HTTP_201_CREATED)
def create_obligation(request: Request, payload: ObligationCreate) -> ObligationDetail:
    """Manual creation. The principal added a task directly through the UI."""
    db = request.app.state.db
    ob = Obligation(
        title=payload.title,
        body=payload.body,
        status=ObligationStatus.inbox,
        owner=ObligationOwner.principal,  # manual = principal owns by default
        source=ObligationSource.manual,
        priority=payload.priority,
        completion_criteria=payload.completion_criteria,
        due_at=payload.due_at,
    )
    with db.session() as s:
        s.add(ob)
        s.commit()
        s.refresh(ob)
    return ObligationDetail.model_validate(ob, from_attributes=True)


@router.patch("/{obligation_id}", response_model=ObligationDetail)
def patch_obligation(
    request: Request, obligation_id: str, payload: ObligationPatch
) -> ObligationDetail:
    """Move an obligation across the board (status), reassign (owner),
    re-prioritize, or rename."""
    db = request.app.state.db
    with db.session() as s:
        ob = s.get(Obligation, obligation_id)
        if ob is None:
            raise HTTPException(status_code=404, detail=f"obligation {obligation_id!r} not found")
        if payload.status is not None:
            ob.status = payload.status
            # Side-effects on status transitions
            if payload.status == ObligationStatus.in_progress and ob.started_at is None:
                ob.started_at = utcnow()
            if payload.status == ObligationStatus.done and ob.completed_at is None:
                ob.completed_at = utcnow()
        if payload.owner is not None:
            ob.owner = payload.owner
        if payload.priority is not None:
            ob.priority = payload.priority
        if payload.title is not None:
            ob.title = payload.title
        if payload.body is not None:
            ob.body = payload.body
        ob.updated_at = utcnow()
        s.add(ob)
        s.commit()
        s.refresh(ob)
    return ObligationDetail.model_validate(ob, from_attributes=True)


@router.delete("/{obligation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_obligation(
    request: Request,
    obligation_id: str,
    hard: Annotated[
        bool, Query(description="Bypass soft-archive (requires aggressive autonomy)")
    ] = False,
) -> None:
    """Archive (soft per L23) or hard-delete an obligation.

    Soft archive moves status to ``done`` and stamps ``completed_at`` —
    reversible by PATCHing back to a different status.

    Hard delete only succeeds when settings.autonomy.archive_instead_of_delete
    is False, OR when the request explicitly passes ``hard=true`` AND the
    settings permit it. Cautious/balanced installs reject hard delete entirely.
    """
    db = request.app.state.db
    settings = _resolved_settings(request)
    archive_only = settings.autonomy.archive_instead_of_delete

    with db.session() as s:
        ob = s.get(Obligation, obligation_id)
        if ob is None:
            raise HTTPException(status_code=404, detail=f"obligation {obligation_id!r} not found")

        if hard and archive_only:
            raise HTTPException(
                status_code=409,
                detail=(
                    "hard delete refused: settings.autonomy.archive_instead_of_delete=true. "
                    "Either soft-archive (omit hard=true) or change to the aggressive preset."
                ),
            )

        if hard:
            s.delete(ob)
        else:
            ob.status = ObligationStatus.done
            ob.completed_at = ob.completed_at or utcnow()
            s.add(ob)
        s.commit()


# ── Helpers ────────────────────────────────────────────────────────────────


def _resolved_settings(request: Request):
    """Return the AgentSettings the app was built with (or holds via manager)."""
    s = request.app.state.settings
    return getattr(s, "settings", s)  # SettingsManager → AgentSettings
