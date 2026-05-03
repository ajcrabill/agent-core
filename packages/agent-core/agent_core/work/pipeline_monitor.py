"""Pipeline monitor — stalled-task detection.

Lifted from Esby's `pipeline_monitor.py` (the "60-minute rule"). Periodically
scans for obligations that have been in_progress or waiting too long without
movement. Stalled obligations get surfaced as Incidents — which then appear
in the agent's context-loader bundle on the next invocation, so they can't
slip silently.

Loriah today has 6 stalled obligations (one 24+ days past due) precisely
because she has no equivalent of this. Sprint 3 fixes that.

What "stalled" means here:
  - Status is in_progress or waiting
  - updated_at is older than the configured threshold for that status
    (defaults: in_progress = 24h; waiting = 7d)
  - Optionally: due_at has passed without completion (any status)

Default thresholds match a personal-CoS rhythm — tunable per install (the
wizard exposes these as Tier 3 settings).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import (
    Incident,
    IncidentSeverity,
    IncidentStatus,
    Obligation,
    ObligationOwner,
    ObligationStatus,
    utcnow,
)

logger = logging.getLogger(__name__)


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class StalledObligation:
    """One obligation flagged as stalled, with the reason it qualified."""

    obligation: Obligation
    reason: str  # 'in_progress_too_long' | 'waiting_too_long' | 'past_due'
    age_hours: float


@dataclass
class StalledScanResult:
    """Outcome of one scan call."""

    stalled: list[StalledObligation] = field(default_factory=list)
    incidents_created: int = 0
    incidents_already_open: int = 0


# ── Monitor ──────────────────────────────────────────────────────────────────


class PipelineMonitor:
    """Scan for stalled obligations and surface them as incidents.

    Idempotent: re-running the scan doesn't create duplicate incidents for
    the same stalled obligation. We dedup by (related_obligation_id, source)
    on Incident — if an open incident already exists for the same obligation
    with source='pipeline_monitor', we don't create another.

    Default thresholds (in hours):
      in_progress_threshold_hours = 24    (a day)
      waiting_threshold_hours = 168       (a week)

    Tunable in the wizard (Tier 3) or per-install via config.
    """

    def __init__(
        self,
        db: Database,
        *,
        in_progress_threshold_hours: float = 24,
        waiting_threshold_hours: float = 168,
        check_due_dates: bool = True,
    ) -> None:
        self.db = db
        self.in_progress_threshold_hours = in_progress_threshold_hours
        self.waiting_threshold_hours = waiting_threshold_hours
        self.check_due_dates = check_due_dates

    @classmethod
    def from_settings(
        cls,
        settings: object,
        db: "Database",
        *,
        check_due_dates: bool = True,
    ) -> "PipelineMonitor":
        """Build from ``AgentSettings``: reads ``settings.work.pipeline_*``."""
        w = settings.work  # type: ignore[attr-defined]
        return cls(
            db,
            in_progress_threshold_hours=w.pipeline_in_progress_threshold_hours,
            waiting_threshold_hours=w.pipeline_waiting_threshold_hours,
            check_due_dates=check_due_dates,
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def find_stalled(self) -> list[StalledObligation]:
        """Return the current stalled set without creating any incidents.

        Useful for the OB UI's "needs attention" panel and for tests.
        """
        now = utcnow()
        out: list[StalledObligation] = []

        with self.db.session() as s:
            # Agent-owned, not done, in a status we monitor
            stmt = (
                select(Obligation)
                .where(Obligation.owner == ObligationOwner.agent)
                .where(Obligation.status != ObligationStatus.done)
            )
            rows = list(s.exec(stmt).all())

        for ob in rows:
            reasons: list[tuple[str, float]] = []  # (reason, age_hours)

            updated = _aware(ob.updated_at)
            age_h = (now - updated).total_seconds() / 3600.0

            if (
                ob.status == ObligationStatus.in_progress
                and age_h > self.in_progress_threshold_hours
            ):
                reasons.append(("in_progress_too_long", age_h))
            if ob.status == ObligationStatus.waiting and age_h > self.waiting_threshold_hours:
                reasons.append(("waiting_too_long", age_h))
            if self.check_due_dates and ob.due_at is not None:
                due = _aware(ob.due_at)
                if due < now:
                    overdue_h = (now - due).total_seconds() / 3600.0
                    reasons.append(("past_due", overdue_h))

            # First reason wins for the surfaced one (ranked by severity:
            # past_due > in_progress_too_long > waiting_too_long)
            if reasons:
                # sort by predefined severity
                sev = {"past_due": 0, "in_progress_too_long": 1, "waiting_too_long": 2}
                reasons.sort(key=lambda r: sev.get(r[0], 9))
                top = reasons[0]
                out.append(
                    StalledObligation(
                        obligation=ob,
                        reason=top[0],
                        age_hours=top[1],
                    )
                )
        return out

    def scan_and_record(self) -> StalledScanResult:
        """Find stalled obligations and create Incidents for any not already
        flagged. Returns counts + the stalled list."""
        result = StalledScanResult()
        stalled = self.find_stalled()
        result.stalled = stalled

        with self.db.session() as s:
            for item in stalled:
                # Dedup: only one open pipeline_monitor incident per obligation
                existing = s.exec(
                    select(Incident)
                    .where(Incident.related_obligation_id == item.obligation.id)
                    .where(Incident.source == "pipeline_monitor")
                    .where(
                        (Incident.status == IncidentStatus.open)
                        | (Incident.status == IncidentStatus.acknowledged)
                    )
                ).first()
                if existing is not None:
                    result.incidents_already_open += 1
                    continue

                inc = Incident(
                    title=_incident_title(item),
                    description=_incident_description(item),
                    severity=_severity_for(item),
                    status=IncidentStatus.open,
                    related_obligation_id=item.obligation.id,
                    source="pipeline_monitor",
                    payload={
                        "reason": item.reason,
                        "age_hours": round(item.age_hours, 1),
                        "obligation_status": item.obligation.status.value,
                    },
                )
                s.add(inc)
                result.incidents_created += 1
            s.commit()

        logger.info(
            "pipeline scan: %d stalled, %d new incidents, %d already open",
            len(stalled),
            result.incidents_created,
            result.incidents_already_open,
        )
        return result


# ── Helpers ──────────────────────────────────────────────────────────────────


def _aware(dt):  # type: ignore[no-untyped-def]
    """Make a datetime timezone-aware (assume UTC if naïve).

    SQLite stores datetimes as naïve strings; this normalizes for comparison
    against utcnow() (which is timezone-aware).
    """
    if dt is None:
        return dt
    if dt.tzinfo is None:
        from datetime import UTC

        return dt.replace(tzinfo=UTC)
    return dt


def _severity_for(item: StalledObligation) -> IncidentSeverity:
    """Severity escalation by how stalled / how overdue."""
    if item.reason == "past_due":
        if item.age_hours > 7 * 24:
            return IncidentSeverity.high
        if item.age_hours > 24:
            return IncidentSeverity.medium
        return IncidentSeverity.low
    # in-progress or waiting too long
    if item.age_hours > 14 * 24:
        return IncidentSeverity.high
    if item.age_hours > 3 * 24:
        return IncidentSeverity.medium
    return IncidentSeverity.low


def _incident_title(item: StalledObligation) -> str:
    if item.reason == "past_due":
        return f"Obligation past due: {item.obligation.title[:80]}"
    if item.reason == "in_progress_too_long":
        return f"Obligation stalled in-progress: {item.obligation.title[:80]}"
    return f"Obligation waiting too long: {item.obligation.title[:80]}"


def _incident_description(item: StalledObligation) -> str:
    days = item.age_hours / 24
    return (
        f"This obligation has been '{item.obligation.status.value}' for "
        f"{days:.1f} days (threshold exceeded). The agent should re-plan, "
        f"escalate, or close it. Obligation id: {item.obligation.id}"
    )


__all__ = ["PipelineMonitor", "StalledObligation", "StalledScanResult"]
