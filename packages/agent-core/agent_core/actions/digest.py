"""Daily digest synthesis.

Per L9 reporting cadence: autonomous actions are reported daily by default
(real-time only on user request). This module aggregates the past 24h of
action_log rows into a markdown summary the user can read at a glance.

What the digest highlights:
  - Counts by action class (so the user sees the shape of the agent's day)
  - Closures: obligations the agent finished
  - Failures: actions that errored, with their related obligations
  - Notable autonomous actions: external email sends, content publishes,
    anything that touched the outside world
  - Open incidents (still surfaced from earlier; reminded here for context)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import (
    ActionLog,
    ActionOutcome,
    Identity,
    Incident,
    IncidentStatus,
    Obligation,
    ObligationEvent,
    ObligationStatus,
    utcnow,
)

# ── Data model ───────────────────────────────────────────────────────────────


@dataclass
class DailyDigest:
    """One day's summary, ready to render or email."""

    period_start: datetime
    period_end: datetime
    instance_name: str | None = None

    actions_total: int = 0
    actions_succeeded: int = 0
    actions_failed: int = 0
    actions_blocked_by_policy: int = 0
    actions_deferred: int = 0
    actions_by_class: dict[str, int] = field(default_factory=dict)

    closed_obligations: list[dict] = field(default_factory=list)
    failed_actions: list[dict] = field(default_factory=list)
    notable_external: list[dict] = field(default_factory=list)
    open_incidents: list[dict] = field(default_factory=list)

    # Autonomous-tick activity (sprints 16/17): triage decisions made by the
    # agent + incidents opened by stalled-detection in this window.
    triage_decisions: list[dict] = field(default_factory=list)
    triage_by_action: dict[str, int] = field(default_factory=dict)
    new_incidents: list[dict] = field(default_factory=list)

    # External action classes worth highlighting (touched the outside world)
    EXTERNAL_CLASSES = (
        "send_email_external",
        "content_publish",
        "calendar_invite_external",
        "cross_agent_message",
    )

    def as_markdown(self) -> str:
        return _render_markdown(self)


# ── Builder ──────────────────────────────────────────────────────────────────


class DailyDigestBuilder:
    """Aggregate the last 24h of activity into a DailyDigest.

    Accepts a custom ``period_hours`` (default 24) for the rare case the
    user wants a different cadence (the wizard exposes this).
    """

    def __init__(self, db: Database, *, period_hours: float = 24) -> None:
        self.db = db
        self.period_hours = period_hours

    @classmethod
    def from_settings(cls, settings: object, db: Database) -> "DailyDigestBuilder":
        """Build from ``AgentSettings``: reads ``settings.notifications.digest_period_hours``."""
        return cls(db, period_hours=settings.notifications.digest_period_hours)  # type: ignore[attr-defined]

    def build(self, *, ending_at: datetime | None = None) -> DailyDigest:
        end = ending_at or utcnow()
        start = end - timedelta(hours=self.period_hours)

        with self.db.session() as s:
            actions = list(
                s.exec(
                    select(ActionLog)
                    .where(ActionLog.occurred_at >= start)
                    .where(ActionLog.occurred_at <= end)
                    .order_by(ActionLog.occurred_at.asc())
                ).all()
            )
            ident = s.get(Identity, "self")
            instance_name = ident.instance_name if ident else None

            # Closed obligations in the window
            closed_obs = list(
                s.exec(
                    select(Obligation)
                    .where(Obligation.status == ObligationStatus.done)
                    .where(Obligation.completed_at >= start)
                    .where(Obligation.completed_at <= end)
                ).all()
            )

            open_incs = list(
                s.exec(
                    select(Incident).where(
                        (Incident.status == IncidentStatus.open)
                        | (Incident.status == IncidentStatus.acknowledged)
                    )
                ).all()
            )

            # Triage decisions in window — ObligationEvent rows from the
            # autonomous tick's email-triage step (Sprint 17).
            triage_events = list(
                s.exec(
                    select(ObligationEvent)
                    .where(ObligationEvent.actor == "agent-triage")
                    .where(ObligationEvent.kind == "comment")
                    .where(ObligationEvent.occurred_at >= start)
                    .where(ObligationEvent.occurred_at <= end)
                    .order_by(ObligationEvent.occurred_at.asc())
                ).all()
            )

            # Incidents newly opened in this window (stalled-detection from
            # Sprint 16 records these).
            new_incs = list(
                s.exec(
                    select(Incident)
                    .where(Incident.occurred_at >= start)
                    .where(Incident.occurred_at <= end)
                    .order_by(Incident.occurred_at.asc())
                ).all()
            )

            # Joined obligation titles for triage decisions (so the digest
            # reads "draft: Email from boss@..." not "draft: ob 4d37ec4b").
            triage_ob_ids = [e.obligation_id for e in triage_events]
            triage_obs: dict[str, Obligation] = {}
            if triage_ob_ids:
                rows = list(
                    s.exec(
                        select(Obligation).where(Obligation.id.in_(triage_ob_ids))
                    ).all()
                )
                triage_obs = {ob.id: ob for ob in rows}

        digest = DailyDigest(
            period_start=start,
            period_end=end,
            instance_name=instance_name,
        )

        # Counts
        digest.actions_total = len(actions)
        outcome_counter: Counter[str] = Counter(a.outcome.value for a in actions)
        digest.actions_succeeded = outcome_counter.get(ActionOutcome.succeeded.value, 0)
        digest.actions_failed = outcome_counter.get(ActionOutcome.failed.value, 0)
        digest.actions_blocked_by_policy = outcome_counter.get(
            ActionOutcome.blocked_by_policy.value, 0
        )
        digest.actions_deferred = outcome_counter.get(ActionOutcome.deferred.value, 0)
        class_counter: Counter[str] = Counter(a.action_class.value for a in actions)
        digest.actions_by_class = dict(class_counter)

        # Closures
        for ob in closed_obs:
            digest.closed_obligations.append(
                {
                    "id": ob.id,
                    "title": ob.title,
                    "completed_at": ob.completed_at,
                }
            )

        # Failures
        for a in actions:
            if a.outcome == ActionOutcome.failed:
                digest.failed_actions.append(
                    {
                        "occurred_at": a.occurred_at,
                        "action_class": a.action_class.value,
                        "obligation_id": a.obligation_id,
                        "target": a.target,
                        "error": a.error,
                        "rationale": a.rationale,
                    }
                )

        # Notable external actions (outside-world touches)
        for a in actions:
            if a.action_class.value in DailyDigest.EXTERNAL_CLASSES:
                digest.notable_external.append(
                    {
                        "occurred_at": a.occurred_at,
                        "action_class": a.action_class.value,
                        "outcome": a.outcome.value,
                        "target": a.target,
                        "obligation_id": a.obligation_id,
                        "rationale": a.rationale,
                    }
                )

        # Open incidents (carry over)
        for i in open_incs:
            digest.open_incidents.append(
                {
                    "id": i.id,
                    "title": i.title,
                    "severity": i.severity.value,
                    "status": i.status.value,
                    "obligation_id": i.related_obligation_id,
                }
            )

        # Triage decisions (Sprint 17 autonomous tick)
        triage_action_counter: Counter[str] = Counter()
        for e in triage_events:
            payload = e.payload or {}
            action = payload.get("action", "?")
            ob = triage_obs.get(e.obligation_id) if e.obligation_id else None
            digest.triage_decisions.append(
                {
                    "occurred_at": e.occurred_at,
                    "obligation_id": e.obligation_id,
                    "obligation_title": ob.title if ob else None,
                    "action": action,
                    "confidence": payload.get("confidence"),
                    "reasoning": payload.get("reasoning"),
                    "status_changed": payload.get("status_changed", False),
                }
            )
            triage_action_counter[action] += 1
        digest.triage_by_action = dict(triage_action_counter)

        # Newly opened incidents in window (Sprint 16 stalled-detection)
        for i in new_incs:
            digest.new_incidents.append(
                {
                    "id": i.id,
                    "title": i.title,
                    "severity": i.severity.value,
                    "status": i.status.value,
                    "source": i.source,
                    "obligation_id": i.related_obligation_id,
                    "occurred_at": i.occurred_at,
                }
            )

        return digest


# ── Markdown rendering ───────────────────────────────────────────────────────


def _render_markdown(d: DailyDigest) -> str:
    lines: list[str] = []
    who = d.instance_name or "your agent"
    lines.append(f"# Daily digest from {who}")
    lines.append("")
    lines.append(f"Period: {d.period_start.isoformat()} → {d.period_end.isoformat()}")
    lines.append("")

    # Headline
    lines.append(
        f"**{d.actions_total} actions** · "
        f"{d.actions_succeeded} succeeded, "
        f"{d.actions_failed} failed, "
        f"{d.actions_blocked_by_policy} blocked by policy, "
        f"{d.actions_deferred} deferred"
    )
    lines.append("")

    # Triage activity (autonomous tick — Sprint 17)
    if d.triage_decisions:
        lines.append(f"## Auto-triage decisions ({len(d.triage_decisions)})")
        lines.append("")
        if d.triage_by_action:
            summary = ", ".join(
                f"{count} {action}" for action, count in sorted(d.triage_by_action.items())
            )
            lines.append(f"_{summary}_")
            lines.append("")
        for t in d.triage_decisions:
            title = t["obligation_title"] or f"obligation {(t['obligation_id'] or '?')[:8]}"
            conf = t.get("confidence")
            conf_str = f" (confidence={conf:.2f})" if isinstance(conf, (int, float)) else ""
            arrow = " → status changed" if t.get("status_changed") else ""
            reasoning = t.get("reasoning") or ""
            line = f"- **{t['action']}**{conf_str}{arrow}: {title}"
            if reasoning:
                snippet = reasoning[:120].replace("\n", " ")
                line += f" — _{snippet}_"
            lines.append(line)
        lines.append("")

    # Newly opened incidents (stalled-detection — Sprint 16)
    if d.new_incidents:
        lines.append(f"## Newly opened incidents ({len(d.new_incidents)})")
        lines.append("")
        for i in d.new_incidents:
            src = f" `source:{i['source']}`" if i.get("source") else ""
            lines.append(
                f"- **{i['title']}** _{i['severity']}_{src} `id:{i['id'][:8]}`"
            )
        lines.append("")

    # Closures
    if d.closed_obligations:
        lines.append(f"## Closed obligations ({len(d.closed_obligations)})")
        lines.append("")
        for ob in d.closed_obligations:
            lines.append(f"- **{ob['title']}** `id:{ob['id'][:8]}`")
        lines.append("")

    # Failures
    if d.failed_actions:
        lines.append(f"## Failures ({len(d.failed_actions)})")
        lines.append("")
        for f in d.failed_actions:
            target = f["target"] or "(no target)"
            err = (f["error"] or "(no error message)")[:200]
            lines.append(
                f"- `{f['action_class']}` on {target} → {err} "
                f"`obligation:{(f['obligation_id'] or '?')[:8]}`"
            )
        lines.append("")

    # External actions
    if d.notable_external:
        lines.append(f"## External-facing actions ({len(d.notable_external)})")
        lines.append("")
        for e in d.notable_external:
            outcome_marker = "✓" if e["outcome"] == "succeeded" else "✗"
            target = e["target"] or "(no target)"
            rationale = e["rationale"] or ""
            lines.append(
                f"- {outcome_marker} `{e['action_class']}` → {target}"
                + (f" — _{rationale}_" if rationale else "")
            )
        lines.append("")

    # Action class breakdown
    if d.actions_by_class:
        lines.append("## By action class")
        lines.append("")
        for cls, count in sorted(d.actions_by_class.items()):
            lines.append(f"- `{cls}`: {count}")
        lines.append("")

    # Open incidents (carryover from earlier)
    if d.open_incidents:
        lines.append(f"## Open incidents ({len(d.open_incidents)})")
        lines.append("")
        for i in d.open_incidents:
            lines.append(
                f"- **{i['title']}** _{i['severity']}_ · _{i['status']}_ `id:{i['id'][:8]}`"
            )
        lines.append("")

    if (
        d.actions_total == 0
        and not d.closed_obligations
        and not d.open_incidents
        and not d.triage_decisions
        and not d.new_incidents
    ):
        lines.append("_Nothing to report — no actions in this window._")
        lines.append("")

    return "\n".join(lines)


__all__ = ["DailyDigest", "DailyDigestBuilder"]
