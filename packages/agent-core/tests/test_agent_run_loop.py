"""Tests for ``dcos run`` — periodic agent tick.

Single-tick behavior is verified end-to-end. The full ``run_loop`` is
exercised with ``once=True`` to avoid actually sleeping in tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_core.agent.run_loop import TickReport, run_loop, run_tick
from agent_core.notifications import NotificationDispatcher, Urgency
from agent_core.notifications.transports import NoopTransport
from agent_core.settings import AgentSettings
from agent_core.state import Database, Obligation, ObligationSource, ObligationStatus
from agent_core.state.models import Incident, IncidentStatus, utcnow
from sqlmodel import select


# ── Fixtures ────────────────────────────────────────────────────────────────


def _db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


def _stalled_obligation(
    *, hours_old: float = 30.0, status: ObligationStatus = ObligationStatus.in_progress
) -> Obligation:
    """Build an Obligation timestamped enough hours in the past to be stalled
    under default settings (24h threshold)."""
    timestamp = utcnow() - timedelta(hours=hours_old)
    return Obligation(
        title=f"Stalled {hours_old}h",
        source=ObligationSource.manual,
        status=status,
        created_at=timestamp,
        updated_at=timestamp,
    )


# ── run_tick: pipeline scan ────────────────────────────────────────────────


def test_run_tick_idle_when_no_obligations() -> None:
    db = _db()
    report = run_tick(db=db, settings=AgentSettings())
    assert report.stalled_total == 0
    assert report.incidents_created == 0
    assert report.notifications_sent == 0
    assert report.errors == []


def test_run_tick_creates_incident_for_stalled() -> None:
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=30))
        s.commit()
    report = run_tick(db=db, settings=AgentSettings())
    assert report.stalled_total == 1
    assert report.incidents_created == 1


def test_run_tick_idempotent_on_repeat() -> None:
    """Second tick on same stalled obligation shouldn't create a duplicate
    incident — already_open count goes up, created stays at 0."""
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=30))
        s.commit()

    settings = AgentSettings()
    r1 = run_tick(db=db, settings=settings, tick_number=1)
    r2 = run_tick(db=db, settings=settings, tick_number=2)

    assert r1.incidents_created == 1
    assert r2.incidents_created == 0
    assert r2.incidents_already_open == 1


def test_run_tick_threshold_settings_respected() -> None:
    """Tighten the threshold and obligations not previously stalled
    become stalled."""
    db = _db()
    with db.session() as s:
        # 5h old — default 24h threshold says not stalled
        s.add(_stalled_obligation(hours_old=5))
        s.commit()

    # Default settings → no stalled
    r1 = run_tick(db=db, settings=AgentSettings())
    assert r1.stalled_total == 0

    # Tighten to 1h → stalled
    tight = AgentSettings(
        work={"pipeline_in_progress_threshold_hours": 1}  # type: ignore[arg-type]
    )
    r2 = run_tick(db=db, settings=tight)
    assert r2.stalled_total == 1


def test_run_tick_handles_pipeline_failure_gracefully() -> None:
    """A scan exception lands in report.errors; tick still returns."""

    class _BoomMonitor:
        def scan_and_record(self):
            raise RuntimeError("db disappeared")

    report = run_tick(
        db=_db(),
        settings=AgentSettings(),
        pipeline_monitor=_BoomMonitor(),
    )
    assert report.errors
    assert "db disappeared" in report.errors[0]
    assert report.stalled_total == 0


# ── run_tick: notifications ────────────────────────────────────────────────


class _RecordingTransport:
    """Transport that records every notification sent — for assertions."""

    name = "recording"

    def __init__(self, *, succeed: bool = True) -> None:
        self.calls: list[dict] = []
        self.succeed = succeed

    def send(self, title, body, *, priority=3, tags=None) -> bool:
        self.calls.append({"title": title, "body": body, "priority": priority, "tags": tags or []})
        return self.succeed


def test_run_tick_sends_notification_for_newly_stalled() -> None:
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=30))
        s.commit()

    transport = _RecordingTransport()
    dispatcher = NotificationDispatcher(
        transport, enabled=True, urgency_floor=Urgency.info
    )
    report = run_tick(db=db, settings=AgentSettings(), dispatcher=dispatcher)

    assert report.notifications_sent == 1
    assert len(transport.calls) == 1
    assert "Stalled" in transport.calls[0]["title"]


def test_run_tick_no_notification_when_dispatcher_none() -> None:
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=30))
        s.commit()
    report = run_tick(db=db, settings=AgentSettings(), dispatcher=None)
    assert report.notifications_sent == 0


def test_run_tick_skips_notification_for_already_open_incident() -> None:
    """The dispatcher only fires for NEWLY-stalled obligations on this tick.
    Already-open incidents are quiet — they made noise when first flagged."""
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=30))
        s.commit()

    transport = _RecordingTransport()
    dispatcher = NotificationDispatcher(
        transport, enabled=True, urgency_floor=Urgency.info
    )
    settings = AgentSettings()

    # First tick: notify
    r1 = run_tick(db=db, settings=settings, dispatcher=dispatcher)
    assert r1.notifications_sent == 1

    # Second tick: same obligation still stalled, but no new incident → no notify
    r2 = run_tick(db=db, settings=settings, dispatcher=dispatcher)
    assert r2.notifications_sent == 0
    assert len(transport.calls) == 1  # still just the first


def test_run_tick_severity_critical_for_very_old_stalled() -> None:
    """Stalled >168h (one week) → critical urgency."""
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=200))  # >168h
        s.commit()

    transport = _RecordingTransport()
    dispatcher = NotificationDispatcher(
        transport, enabled=True, urgency_floor=Urgency.info
    )
    run_tick(db=db, settings=AgentSettings(), dispatcher=dispatcher)
    # Critical maps to ntfy priority 5
    assert transport.calls[0]["priority"] == int(Urgency.critical)


def test_run_tick_records_dropped_when_below_floor() -> None:
    """Dispatcher with critical-only floor + warn urgency → dropped, not sent."""
    db = _db()
    with db.session() as s:
        # 30h old → warn (not critical), but a new incident
        s.add(_stalled_obligation(hours_old=30))
        s.commit()

    dispatcher = NotificationDispatcher(
        NoopTransport(), enabled=True, urgency_floor=Urgency.critical
    )
    report = run_tick(db=db, settings=AgentSettings(), dispatcher=dispatcher)
    # 30h is below 168h → warn urgency → below critical floor → dropped
    assert report.notifications_sent == 0
    assert report.notifications_dropped == 1


# ── run_loop: --once + tick counting ───────────────────────────────────────


def test_run_loop_once_returns_after_single_tick() -> None:
    db = _db()
    received: list[TickReport] = []
    count = run_loop(
        db=db,
        settings=AgentSettings(),
        once=True,
        on_tick=received.append,
    )
    assert count == 1
    assert len(received) == 1
    assert received[0].tick_number == 1


def test_run_loop_on_tick_callback_fires_per_tick() -> None:
    """If the callback raises, the loop must continue (don't crash on logger
    bugs)."""
    db = _db()
    seen: list[int] = []

    def callback(report: TickReport) -> None:
        seen.append(report.tick_number)
        raise RuntimeError("logger bug")

    count = run_loop(
        db=db,
        settings=AgentSettings(),
        once=True,
        on_tick=callback,
    )
    assert count == 1
    assert seen == [1]


def test_run_loop_with_empty_db_idle_tick() -> None:
    """Empty db → tick reports zero everything → loop returns cleanly."""
    db = _db()
    received: list[TickReport] = []
    run_loop(db=db, settings=AgentSettings(), once=True, on_tick=received.append)
    assert received[0].stalled_total == 0
    assert received[0].notifications_sent == 0
