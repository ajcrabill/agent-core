"""Periodic agent loop — `dcos run`.

The agent does work on its own, on a schedule, even when no human is
chatting with it. v1 scope (intentionally narrow):

  Each tick:
    1. PipelineMonitor.scan_and_record() — finds stalled obligations,
       opens Incident rows for newly-stalled ones (idempotent — won't
       re-flag already-open ones).
    2. For each newly-stalled obligation, send a notification via
       NotificationDispatcher (respects the user's urgency_floor;
       falls through to the daily digest if push is disabled).
    3. Sleep ``interval`` seconds.
    4. Repeat until SIGINT.

Future ticks (sprint 17+) will:
  - Auto-triage inbox-status obligations using the email-triage skill
  - Run the daily digest on a 24h cadence
  - Drive the AgentLoop.tick() (plan/execute/verify) when Hermes-style
    skills are wired

Lifecycle:
  - Foreground process (like ``dcos serve``) — blocks the terminal,
    Ctrl-C exits cleanly.
  - ``--once`` does a single tick + exits (CI / cron-driven setups).
  - ``--interval`` controls sleep between ticks (default 300s = 5min).
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Result types ───────────────────────────────────────────────────────────


@dataclass
class TriageReport:
    """Subset of tick activity: how many inbox-status email obligations got
    auto-classified this tick by the email-triage skill."""

    candidates: int = 0
    """Inbox+email obligations eligible for triage this tick."""
    triaged: int = 0
    """How many actually got a triage decision (rest skipped because
    already-triaged or LLM error)."""
    by_action: dict[str, int] = field(default_factory=dict)
    """Counts per email-triage action: flag/archive/hold/draft/…"""
    skipped_already_triaged: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class TickReport:
    """One tick's outcome — useful for logs + dashboards."""

    tick_number: int
    stalled_total: int
    incidents_created: int
    incidents_already_open: int
    notifications_sent: int
    notifications_dropped: int
    duration_seconds: float
    errors: list[str]
    triage: TriageReport | None = None
    # Sprint 20: scheduled digest delivery
    digest_delivery_attempted: bool = False
    digest_delivery_sent: bool = False
    digest_delivery_reason: str | None = None
    # Sprint 21: IMAP email ingestion
    email_fetched: int = 0
    email_captured: int = 0
    email_skipped_duplicate: int = 0
    # Sprint 22: draft composition (sending stays manual / explicit)
    drafts_composed: int = 0
    drafts_skipped_already_drafted: int = 0


# ── Single tick ────────────────────────────────────────────────────────────


def run_tick(
    *,
    db: Any,
    settings: Any,
    dispatcher: Any | None = None,
    pipeline_monitor: Any | None = None,
    language_model: Any | None = None,
    triage_enabled: bool = True,
    triage_limit: int = 20,
    tick_number: int = 0,
    digest_delivery_enabled: bool = True,
    email_fetch_enabled: bool = True,
    compose_enabled: bool = True,
) -> TickReport:
    """One iteration of the autonomous loop.

    Args:
        db: agent-core Database.
        settings: AgentSettings (or SettingsManager — drilled into ``.settings``).
        dispatcher: NotificationDispatcher. None → no notifications + no digest delivery.
        pipeline_monitor: pre-built PipelineMonitor. None → built from settings.
        language_model: pre-built LanguageModel for triage. None → built from
            settings + secrets store. Triage skipped if construction fails.
        triage_enabled: If True (default), auto-triage inbox-status email
            obligations using the email-triage skill.
        triage_limit: Max obligations to triage per tick (back-pressure on
            large inboxes — trickle through over multiple ticks).
        tick_number: monotonic counter for logs.
        digest_delivery_enabled: If True (default) and a dispatcher is given,
            attempt cadenced digest delivery at the end of each tick. The
            cadence is governed by settings.notifications.digest_period_hours;
            most ticks will return ``skipped_too_recent``.

    Returns a ``TickReport``. Never raises — errors land in ``report.errors``.
    """
    from agent_core.notifications import Notification, Urgency
    from agent_core.work.pipeline_monitor import PipelineMonitor

    started = time.time()
    settings_obj = getattr(settings, "settings", settings)
    errors: list[str] = []

    if pipeline_monitor is None:
        pipeline_monitor = PipelineMonitor.from_settings(settings_obj, db)

    # 1. Scan for stalled obligations
    try:
        scan_result = pipeline_monitor.scan_and_record()
    except Exception as e:
        logger.exception("pipeline scan failed (tick %d)", tick_number)
        errors.append(f"pipeline scan: {e}")
        scan_result = None

    if scan_result is None:
        return TickReport(
            tick_number=tick_number,
            stalled_total=0,
            incidents_created=0,
            incidents_already_open=0,
            notifications_sent=0,
            notifications_dropped=0,
            duration_seconds=time.time() - started,
            errors=errors,
        )

    # 2. Notify on newly-stalled (skip already-open ones — they were
    # already noisy when first flagged).
    sent = 0
    dropped = 0
    if dispatcher is not None and scan_result.incidents_created > 0:
        # Find which stalled items got NEW incidents this tick. The scan
        # result doesn't directly give us this, but we know:
        #   incidents_created = stalled - already_open
        # And the order is preserved in scan_result.stalled.
        new_count = scan_result.incidents_created
        for item in scan_result.stalled[:new_count]:
            try:
                title = f"Stalled: {item.obligation.title}"
                body = (
                    f"{item.reason} for "
                    f"{int(item.age_hours)}h. Status: "
                    f"{item.obligation.status.value}."
                )
                # Severity → urgency mapping
                urgency = Urgency.critical if item.age_hours > 168 else Urgency.warn
                result = dispatcher.notify(
                    Notification(
                        title=title,
                        body=body,
                        urgency=urgency,
                        tags=["stalled-obligation"],
                    )
                )
                if result.delivered:
                    sent += 1
                else:
                    dropped += 1
            except Exception as e:
                logger.exception("notification dispatch failed")
                errors.append(f"notify {item.obligation.id[:8]}: {e}")
                dropped += 1

    # 2.5. Pull new email from IMAP into inbox-status obligations. Runs
    # BEFORE triage so the same tick that fetches a new message also
    # classifies it. Skipped silently when email.imap.enabled is False
    # — that's the default, so this is a no-op for users who haven't
    # turned IMAP on.
    email_fetched = 0
    email_captured = 0
    email_skipped_duplicate = 0
    if email_fetch_enabled and getattr(settings_obj.email.imap, "enabled", False):
        try:
            from agent_core.secrets import default_store
            from agent_core.work.email_fetch import (
                EmailFetcher,
                EmailFetchError,
                fetch_and_capture,
            )

            fetcher = EmailFetcher.from_settings(settings_obj, default_store())
            fetch_report = fetch_and_capture(
                fetcher=fetcher,
                db=db,
                limit=settings_obj.email.imap.fetch_limit,
            )
            email_fetched = fetch_report.fetched
            email_captured = fetch_report.captured
            email_skipped_duplicate = fetch_report.skipped_duplicate
            errors.extend(fetch_report.errors)
        except EmailFetchError as e:
            # Configuration error (missing host / password / etc) —
            # surface but don't break the tick.
            errors.append(f"email fetch skipped: {e}")
        except Exception as e:
            logger.exception("email fetch failed (tick %d)", tick_number)
            errors.append(f"email fetch: {e}")

    # 3. Triage inbox-status email obligations.
    triage_report: TriageReport | None = None
    if triage_enabled:
        triage_report = triage_inbox(
            db=db,
            settings=settings,
            language_model=language_model,
            limit=triage_limit,
        )
        errors.extend(triage_report.errors)

    # 3.5. Draft replies for triaged-as-draft emails. Gated by
    # settings.email.auto_compose so the agent doesn't burn LLM tokens
    # drafting every reply unsolicited. Sends NEVER happen here — drafts
    # only. The user reviews + sends explicitly via CLI / chat.
    drafts_composed = 0
    drafts_skipped = 0
    if compose_enabled and getattr(settings_obj.email, "auto_compose", False):
        try:
            from agent_core.work.email_send import compose_drafts

            compose_report = compose_drafts(
                db=db,
                settings=settings,
                language_model=language_model,
            )
            drafts_composed = compose_report.drafted
            drafts_skipped = compose_report.skipped_already_drafted
            errors.extend(compose_report.errors)
        except Exception as e:
            logger.exception("compose_drafts failed (tick %d)", tick_number)
            errors.append(f"compose: {e}")

    # 4. Cadenced digest delivery. Each tick checks "is it time?" — the
    # delivery helper short-circuits with skipped_too_recent if not. The
    # period is settings.notifications.digest_period_hours (default 24).
    digest_attempted = False
    digest_sent = False
    digest_reason: str | None = None
    if digest_delivery_enabled and dispatcher is not None:
        try:
            from agent_core.actions.digest import (
                DailyDigestBuilder,
                deliver_digest,
            )

            builder = DailyDigestBuilder.from_settings(settings_obj, db)
            delivery = deliver_digest(
                db=db,
                dispatcher=dispatcher,
                builder=builder,
                # Tick deliveries respect both knobs — the user owns
                # cadence + floor via settings. Explicit `dcos digest --send`
                # is the bypass surface.
                force=False,
                bypass_floor=False,
                send_when_empty=False,
            )
            digest_attempted = True
            digest_sent = delivery.sent
            digest_reason = delivery.reason
        except Exception as e:
            logger.exception("digest delivery failed (tick %d)", tick_number)
            errors.append(f"digest delivery: {e}")

    return TickReport(
        tick_number=tick_number,
        stalled_total=len(scan_result.stalled),
        incidents_created=scan_result.incidents_created,
        incidents_already_open=scan_result.incidents_already_open,
        notifications_sent=sent,
        notifications_dropped=dropped,
        duration_seconds=time.time() - started,
        errors=errors,
        triage=triage_report,
        digest_delivery_attempted=digest_attempted,
        digest_delivery_sent=digest_sent,
        digest_delivery_reason=digest_reason,
        email_fetched=email_fetched,
        email_captured=email_captured,
        email_skipped_duplicate=email_skipped_duplicate,
        drafts_composed=drafts_composed,
        drafts_skipped_already_drafted=drafts_skipped,
    )


# ── Triage step ────────────────────────────────────────────────────────────


# Maps email-triage skill action → ObligationStatus transition.
# - flag: stays inbox (the user needs to look)
# - archive: → done (auto-archive per L23 archive_instead_of_delete)
# - hold: → waiting (revisit in a few days)
# - draft: → in-progress (signals "agent should draft via email-composer")
# - track-relationship: stays inbox (still needs a reply, but People note also
#                         needs creating — separate flow once that lands)
# - task: stays inbox (it IS a task; no auto-transition)
_TRIAGE_TO_STATUS = {
    "flag": None,  # stays inbox
    "archive": "done",
    "hold": "waiting",
    "draft": "in_progress",
    "track-relationship": None,  # stays inbox
    "task": None,  # stays inbox
}


def triage_inbox(
    *,
    db: Any,
    settings: Any,
    language_model: Any | None = None,
    limit: int = 20,
) -> TriageReport:
    """Run email-triage on inbox-status email obligations.

    Idempotent: only triages obligations without a prior triage event.
    The decision is recorded as an ObligationEvent (kind=comment, payload
    type='triage') so subsequent ticks skip them.

    Status transitions per ``_TRIAGE_TO_STATUS``. Confidence below the
    settings.learning.confidence_medium threshold leaves the obligation
    in inbox regardless of action — low-confidence calls need human review.

    Returns a ``TriageReport``. Never raises.
    """
    from sqlmodel import select

    from agent_core.skills import LanguageModelError, language_model_from_settings
    from agent_core.state.models import (
        Obligation,
        ObligationEvent,
        ObligationEventKind,
        ObligationSource,
        ObligationStatus,
        utcnow,
    )

    settings_obj = getattr(settings, "settings", settings)
    report = TriageReport()

    # Build the LanguageModel. If construction fails (no key configured),
    # skip triage silently — the user gets stalled-detection without it.
    if language_model is None:
        try:
            from agent_core.secrets import default_store

            language_model = language_model_from_settings(settings_obj, default_store())
        except LanguageModelError as e:
            report.errors.append(f"triage skipped: LLM not configured ({e})")
            return report

    # Find candidates: inbox + email source
    with db.session() as s:
        stmt = (
            select(Obligation)
            .where(Obligation.status == ObligationStatus.inbox)
            .where(Obligation.source == ObligationSource.inbound_email)
            .order_by(Obligation.priority.desc(), Obligation.created_at)
            .limit(limit * 2)  # over-fetch; idempotency filter happens below
        )
        candidates = list(s.exec(stmt).all())

        # Idempotency: skip obligations that already have a triage event.
        ob_ids = [ob.id for ob in candidates]
        if ob_ids:
            triaged_event_rows = list(
                s.exec(
                    select(ObligationEvent)
                    .where(ObligationEvent.obligation_id.in_(ob_ids))
                    .where(ObligationEvent.actor == "agent-triage")
                ).all()
            )
            already_triaged = {e.obligation_id for e in triaged_event_rows}
        else:
            already_triaged = set()

    fresh = [ob for ob in candidates if ob.id not in already_triaged]
    report.candidates = len(candidates)
    report.skipped_already_triaged = len(candidates) - len(fresh)

    if not fresh:
        return report

    # Run triage on each, up to limit.
    from agent_core.skills import SkillContext, SkillRunner, default_registry

    runner = SkillRunner(default_registry)
    confidence_floor = settings_obj.learning.confidence_medium

    for ob in fresh[:limit]:
        # Email obligations have title="Email from <sender>: <subject>"
        # per InboundCapture.capture_email. Reverse-engineer sender for
        # the skill input.
        sender, subject = _extract_sender_subject(ob.title)
        body = ob.body or ""

        ctx = SkillContext(
            settings=settings_obj,
            db=db,
            language_model=language_model,
        )
        outcome = runner.run(
            "email-triage",
            {"sender": sender, "subject": subject, "body": body},
            ctx,
        )
        if not outcome.succeeded:
            report.errors.append(f"triage {ob.id[:8]}: {outcome.error}")
            continue

        result = outcome.result
        action = result.output.action
        confidence = result.confidence
        report.triaged += 1
        report.by_action[action] = report.by_action.get(action, 0) + 1

        # Confidence-gated transition. Low-confidence stays inbox even for
        # would-be-actionable triage (archive/hold/draft) so the human can
        # review.
        new_status_name = _TRIAGE_TO_STATUS.get(action)
        applied = False
        if new_status_name and confidence >= confidence_floor:
            # Map status string → ObligationStatus enum
            try:
                new_status = ObligationStatus[new_status_name]
            except KeyError:
                new_status = None
            if new_status is not None:
                with db.session() as s:
                    row = s.get(Obligation, ob.id)
                    if row is not None and row.status == ObligationStatus.inbox:
                        row.status = new_status
                        row.updated_at = utcnow()
                        if new_status == ObligationStatus.done:
                            row.completed_at = utcnow()
                        elif new_status == ObligationStatus.in_progress:
                            row.started_at = utcnow()
                        s.add(row)

                        # Status-change event
                        s.add(
                            ObligationEvent(
                                obligation_id=ob.id,
                                kind=ObligationEventKind.status_changed,
                                actor="agent-triage",
                                payload={
                                    "from": ObligationStatus.inbox.value,
                                    "to": new_status.value,
                                    "reason": "auto-triage",
                                },
                            )
                        )
                        s.commit()
                        applied = True

        # Always record a triage decision event (idempotency marker).
        with db.session() as s:
            s.add(
                ObligationEvent(
                    obligation_id=ob.id,
                    kind=ObligationEventKind.comment,
                    actor="agent-triage",
                    payload={
                        "type": "triage",
                        "action": action,
                        "confidence": confidence,
                        "reasoning": result.output.reasoning or "",
                        "status_changed": applied,
                    },
                )
            )
            s.commit()

    return report


def _extract_sender_subject(title: str) -> tuple[str, str]:
    """Pull (sender, subject) out of an obligation title that follows
    InboundCapture.capture_email's format ``"Email from <sender>: <subject>"``.

    Falls through to ``("unknown", title)`` if the format doesn't match —
    the skill still works, just with less context."""
    if title.startswith("Email from "):
        rest = title[len("Email from ") :]
        sep = rest.find(": ")
        if sep > 0:
            return rest[:sep], rest[sep + 2 :]
    return "unknown", title


# ── Main loop ──────────────────────────────────────────────────────────────


def run_loop(
    *,
    db: Any,
    settings: Any,
    dispatcher: Any | None = None,
    interval_seconds: float = 300.0,
    once: bool = False,
    on_tick: Any = None,
) -> int:
    """Run the tick loop until SIGINT (or once if ``once=True``).

    Args:
        db, settings, dispatcher: passed to ``run_tick`` each iteration.
        interval_seconds: sleep between ticks. Default 5 minutes.
        once: single tick + return.
        on_tick: optional callback ``fn(TickReport) -> None`` — for tests
            and custom logging. Default: log to stdout.

    Returns the number of ticks executed (useful in tests + as exit code).
    """
    if on_tick is None:
        on_tick = _default_on_tick

    stop = _StopFlag()
    if not once:
        signal.signal(signal.SIGINT, stop.set)
        signal.signal(signal.SIGTERM, stop.set)

    tick_number = 0
    while not stop.is_set():
        tick_number += 1
        report = run_tick(
            db=db,
            settings=settings,
            dispatcher=dispatcher,
            tick_number=tick_number,
        )
        try:
            on_tick(report)
        except Exception:
            logger.exception("on_tick callback failed")
            # Don't crash the loop if the callback throws.

        if once:
            break

        # Sleep in short slices so SIGINT is responsive.
        slept = 0.0
        while slept < interval_seconds and not stop.is_set():
            chunk = min(0.5, interval_seconds - slept)
            time.sleep(chunk)
            slept += chunk

    return tick_number


# ── Helpers ────────────────────────────────────────────────────────────────


class _StopFlag:
    """Tiny bool wrapper so signal handlers can set it."""

    def __init__(self) -> None:
        self._set = False

    def set(self, *_args: Any) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set


def _default_on_tick(report: TickReport) -> None:
    """Default per-tick logger — terse line on stdout."""
    summary = (
        f"tick {report.tick_number}: "
        f"{report.stalled_total} stalled, "
        f"{report.incidents_created} new incidents, "
        f"{report.notifications_sent} notifications sent"
    )
    if report.notifications_dropped:
        summary += f" ({report.notifications_dropped} dropped)"
    if report.email_captured or report.email_skipped_duplicate:
        summary += f"; email: {report.email_captured} captured" + (
            f", {report.email_skipped_duplicate} dup" if report.email_skipped_duplicate else ""
        )
    if report.triage and report.triage.triaged:
        actions = ", ".join(f"{k}={v}" for k, v in sorted(report.triage.by_action.items()))
        summary += f"; triaged {report.triage.triaged} ({actions})"
    if report.drafts_composed:
        summary += f"; drafted {report.drafts_composed}"
    if report.digest_delivery_attempted:
        if report.digest_delivery_sent:
            summary += "; digest sent"
        elif (
            report.digest_delivery_reason and report.digest_delivery_reason != "skipped_too_recent"
        ):
            # Stay quiet on the noisy default ("skipped_too_recent" is the
            # boring case; only surface unusual reasons).
            summary += f"; digest: {report.digest_delivery_reason}"
    if report.errors:
        summary += f" — errors: {report.errors}"
    logger.info(summary)
    print(summary, flush=True)


__all__ = ["TickReport", "TriageReport", "run_loop", "run_tick", "triage_inbox"]
