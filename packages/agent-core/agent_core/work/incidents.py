"""Incident recorder — single entry point for "something failed" events.

Per the design: every failed tool call, cron error, quality-audit failure,
mesh delivery failure, etc. becomes an Incident row. The context-loader
(Sprint 2) surfaces open incidents at the top of the agent's prompt envelope
so the agent must consult them before claiming completion on related work.

This module is the helper any layer can import to record an incident
without writing repetitive boilerplate.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import (
    Incident,
    IncidentSeverity,
    IncidentStatus,
    utcnow,
)

logger = logging.getLogger(__name__)


class IncidentRecorder:
    """Single entry point for recording / acknowledging / resolving incidents.

    Usage:
        rec = IncidentRecorder(db)
        rec.record(
            title="Gmail OAuth refresh failed",
            source="cron",
            severity=IncidentSeverity.high,
            related_obligation_id="<id>",  # optional
            payload={"refresh_token_expired_at": ts},  # optional
        )

        rec.acknowledge(incident_id)
        rec.resolve(incident_id)
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Record ──────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        title: str,
        source: str,
        severity: IncidentSeverity = IncidentSeverity.medium,
        description: str | None = None,
        related_obligation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedup_open: bool = True,
    ) -> Incident:
        """Create an Incident.

        ``dedup_open`` (default True): if an open incident already exists with
        the same (title, source, related_obligation_id), return that one
        instead of creating a duplicate. Disable for cases where a fresh
        record per occurrence matters (e.g., recurring transient errors).
        """
        with self.db.session() as s:
            if dedup_open:
                existing = self._find_open_match(s, title, source, related_obligation_id)
                if existing is not None:
                    logger.info(
                        "incident dedup: returning existing open id=%s",
                        existing.id,
                    )
                    return existing

            inc = Incident(
                title=title,
                description=description,
                severity=severity,
                status=IncidentStatus.open,
                related_obligation_id=related_obligation_id,
                source=source,
                payload=payload,
            )
            s.add(inc)
            s.commit()
            s.refresh(inc)
            logger.info(
                "incident recorded: id=%s severity=%s source=%s title=%r",
                inc.id,
                inc.severity.value,
                inc.source,
                inc.title,
            )
            return inc

    # ── Lifecycle transitions ──────────────────────────────────────────────

    def acknowledge(self, incident_id: str) -> Incident:
        """Mark an incident as acknowledged (the agent has seen it)."""
        return self._transition(
            incident_id,
            new_status=IncidentStatus.acknowledged,
            stamp_attr="acknowledged_at",
        )

    def resolve(self, incident_id: str, *, note: str | None = None) -> Incident:
        """Mark an incident as resolved (no longer surfaces in context)."""
        with self.db.session() as s:
            inc = s.get(Incident, incident_id)
            if inc is None:
                raise ValueError(f"incident {incident_id!r} not found")
            inc.status = IncidentStatus.resolved
            inc.resolved_at = utcnow()
            if note:
                # Append note to payload for posterity
                payload = dict(inc.payload or {})
                payload.setdefault("resolution_notes", []).append(note)
                inc.payload = payload
            s.add(inc)
            s.commit()
            s.refresh(inc)
        return inc

    # ── Queries ────────────────────────────────────────────────────────────

    def open_for_obligation(self, obligation_id: str) -> list[Incident]:
        """All non-resolved incidents tied to ``obligation_id``."""
        with self.db.session() as s:
            stmt = (
                select(Incident)
                .where(Incident.related_obligation_id == obligation_id)
                .where(Incident.status != IncidentStatus.resolved)
            )
            return list(s.exec(stmt).all())

    def has_open_for_obligation(self, obligation_id: str) -> bool:
        return bool(self.open_for_obligation(obligation_id))

    # ── Internals ──────────────────────────────────────────────────────────

    def _find_open_match(
        self,
        s,  # type: ignore[no-untyped-def]
        title: str,
        source: str,
        related_obligation_id: str | None,
    ) -> Incident | None:
        stmt = (
            select(Incident)
            .where(Incident.title == title)
            .where(Incident.source == source)
            .where(
                (Incident.status == IncidentStatus.open)
                | (Incident.status == IncidentStatus.acknowledged)
            )
        )
        if related_obligation_id is not None:
            stmt = stmt.where(Incident.related_obligation_id == related_obligation_id)
        else:
            stmt = stmt.where(Incident.related_obligation_id.is_(None))
        return s.exec(stmt).first()

    def _transition(
        self,
        incident_id: str,
        *,
        new_status: IncidentStatus,
        stamp_attr: str,
    ) -> Incident:
        with self.db.session() as s:
            inc = s.get(Incident, incident_id)
            if inc is None:
                raise ValueError(f"incident {incident_id!r} not found")
            inc.status = new_status
            setattr(inc, stamp_attr, utcnow())
            s.add(inc)
            s.commit()
            s.refresh(inc)
        return inc


__all__ = ["IncidentRecorder"]
