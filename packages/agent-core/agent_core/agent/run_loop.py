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
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Result types ───────────────────────────────────────────────────────────


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


# ── Single tick ────────────────────────────────────────────────────────────


def run_tick(
    *,
    db: Any,
    settings: Any,
    dispatcher: Any | None = None,
    pipeline_monitor: Any | None = None,
    tick_number: int = 0,
) -> TickReport:
    """One iteration of the autonomous loop.

    Args:
        db: agent-core Database.
        settings: AgentSettings (or SettingsManager — drilled into ``.settings``).
        dispatcher: NotificationDispatcher. None → no notifications.
        pipeline_monitor: pre-built PipelineMonitor. None → built from settings.
        tick_number: monotonic counter for logs.

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
                urgency = (
                    Urgency.critical if item.age_hours > 168 else Urgency.warn
                )
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

    return TickReport(
        tick_number=tick_number,
        stalled_total=len(scan_result.stalled),
        incidents_created=scan_result.incidents_created,
        incidents_already_open=scan_result.incidents_already_open,
        notifications_sent=sent,
        notifications_dropped=dropped,
        duration_seconds=time.time() - started,
        errors=errors,
    )


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
        except Exception as e:
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
    if report.errors:
        summary += f" — errors: {report.errors}"
    logger.info(summary)
    print(summary, flush=True)


__all__ = ["TickReport", "run_loop", "run_tick"]
