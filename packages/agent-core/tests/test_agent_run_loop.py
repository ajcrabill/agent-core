"""Tests for ``dcos run`` — periodic agent tick.

Single-tick behavior is verified end-to-end. The full ``run_loop`` is
exercised with ``once=True`` to avoid actually sleeping in tests.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from agent_core.agent.run_loop import TickReport, run_loop, run_tick
from agent_core.notifications import NotificationDispatcher, Urgency
from agent_core.notifications.transports import NoopTransport
from agent_core.settings import AgentSettings
from agent_core.state import Database, Obligation, ObligationSource, ObligationStatus
from agent_core.state.models import utcnow
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
    dispatcher = NotificationDispatcher(transport, enabled=True, urgency_floor=Urgency.info)
    report = run_tick(
        db=db,
        settings=AgentSettings(),
        dispatcher=dispatcher,
        digest_delivery_enabled=False,  # focus this test on stalled-notification path
    )

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
    dispatcher = NotificationDispatcher(transport, enabled=True, urgency_floor=Urgency.info)
    settings = AgentSettings()

    # First tick: notify (digest delivery disabled to keep this test focused
    # on the stalled-notification path)
    r1 = run_tick(db=db, settings=settings, dispatcher=dispatcher, digest_delivery_enabled=False)
    assert r1.notifications_sent == 1

    # Second tick: same obligation still stalled, but no new incident → no notify
    r2 = run_tick(db=db, settings=settings, dispatcher=dispatcher, digest_delivery_enabled=False)
    assert r2.notifications_sent == 0
    assert len(transport.calls) == 1  # still just the first


def test_run_tick_severity_critical_for_very_old_stalled() -> None:
    """Stalled >168h (one week) → critical urgency."""
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=200))  # >168h
        s.commit()

    transport = _RecordingTransport()
    dispatcher = NotificationDispatcher(transport, enabled=True, urgency_floor=Urgency.info)
    run_tick(
        db=db,
        settings=AgentSettings(),
        dispatcher=dispatcher,
        digest_delivery_enabled=False,
    )
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
    report = run_tick(
        db=db,
        settings=AgentSettings(),
        dispatcher=dispatcher,
        digest_delivery_enabled=False,
    )
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


# ── triage_inbox: auto-classify inbox emails ──────────────────────────────


import json as _json  # noqa: E402 — needs to happen after dcos_agent.skills registers

import dcos_agent.skills  # noqa: F401, E402 — registers email-triage in default_registry
from agent_core.agent.run_loop import triage_inbox  # noqa: E402 — depends on registration
from agent_core.skills import StubLanguageModel  # noqa: E402 — depends on registration
from agent_core.state.models import ObligationEvent  # noqa: E402 — depends on registration


def _email_obligation(*, sender="news@example.com", subject="hello", body="x") -> Obligation:
    return Obligation(
        title=f"Email from {sender}: {subject}",
        body=body,
        source=ObligationSource.inbound_email,
        status=ObligationStatus.inbox,
    )


def _triage_lm(action: str, score: float = 0.95, reasoning: str = "stub") -> StubLanguageModel:
    """Build a StubLanguageModel that returns valid email-triage JSON."""
    return StubLanguageModel(
        default=_json.dumps({"action": action, "score": score, "reasoning": reasoning})
    )


def test_triage_skips_when_no_inbox_emails() -> None:
    db = _db()
    report = triage_inbox(db=db, settings=AgentSettings(), language_model=_triage_lm("flag"))
    assert report.candidates == 0
    assert report.triaged == 0


def test_triage_classifies_inbox_email() -> None:
    db = _db()
    with db.session() as s:
        s.add(_email_obligation(sender="boss@x.com", subject="urgent question"))
        s.commit()

    report = triage_inbox(
        db=db, settings=AgentSettings(), language_model=_triage_lm("flag", score=0.9)
    )
    assert report.candidates == 1
    assert report.triaged == 1
    assert report.by_action == {"flag": 1}


def test_triage_archive_high_confidence_moves_to_done() -> None:
    """High-confidence archive transitions inbox → done."""
    db = _db()
    with db.session() as s:
        ob = _email_obligation(sender="news@nytimes.com", subject="Daily digest")
        s.add(ob)
        s.commit()
        s.refresh(ob)
        ob_id = ob.id

    triage_inbox(db=db, settings=AgentSettings(), language_model=_triage_lm("archive", score=0.95))

    with db.session() as s:
        row = s.get(Obligation, ob_id)
    assert row.status == ObligationStatus.done
    assert row.completed_at is not None


def test_triage_low_confidence_keeps_in_inbox() -> None:
    """Confidence below settings.learning.confidence_medium (0.5) →
    obligation stays in inbox even on archive — human review needed."""
    db = _db()
    with db.session() as s:
        ob = _email_obligation()
        s.add(ob)
        s.commit()
        s.refresh(ob)
        ob_id = ob.id

    triage_inbox(db=db, settings=AgentSettings(), language_model=_triage_lm("archive", score=0.3))

    with db.session() as s:
        row = s.get(Obligation, ob_id)
    assert row.status == ObligationStatus.inbox


def test_triage_idempotent_skip_already_triaged() -> None:
    """A second tick should NOT re-triage. Counts go to skipped_already_triaged."""
    db = _db()
    with db.session() as s:
        s.add(_email_obligation())
        s.commit()

    lm = _triage_lm("flag")
    r1 = triage_inbox(db=db, settings=AgentSettings(), language_model=lm)
    r2 = triage_inbox(db=db, settings=AgentSettings(), language_model=lm)

    assert r1.triaged == 1
    assert r2.triaged == 0
    assert r2.skipped_already_triaged == 1
    assert len(lm.calls) == 1  # second call DIDN'T hit the LM


def test_triage_records_event_with_decision() -> None:
    """Each triage records an ObligationEvent with the decision payload —
    surfaces in the audit trail."""
    db = _db()
    with db.session() as s:
        ob = _email_obligation()
        s.add(ob)
        s.commit()
        s.refresh(ob)
        ob_id = ob.id

    triage_inbox(
        db=db,
        settings=AgentSettings(),
        language_model=_triage_lm("hold", score=0.9, reasoning="not urgent"),
    )

    with db.session() as s:
        events = list(
            s.exec(
                select(ObligationEvent)
                .where(ObligationEvent.obligation_id == ob_id)
                .where(ObligationEvent.actor == "agent-triage")
            ).all()
        )

    # Two events: one status_changed (hold → waiting), one comment (decision)
    kinds = {e.kind.value for e in events}
    assert "status_changed" in kinds
    assert "comment" in kinds

    decision = next(e for e in events if e.kind.value == "comment")
    assert decision.payload["type"] == "triage"
    assert decision.payload["action"] == "hold"
    assert decision.payload["confidence"] == pytest.approx(0.9)
    assert decision.payload["reasoning"] == "not urgent"


def test_triage_respects_limit() -> None:
    """limit caps the number of triages per tick (back-pressure)."""
    db = _db()
    with db.session() as s:
        for i in range(5):
            s.add(_email_obligation(subject=f"msg {i}"))
        s.commit()

    report = triage_inbox(
        db=db,
        settings=AgentSettings(),
        language_model=_triage_lm("flag"),
        limit=2,
    )
    assert report.triaged == 2


def test_triage_skill_failure_surfaces_in_errors() -> None:
    """If email-triage raises (e.g., model returns garbage), error → report
    but other obligations still get processed."""
    db = _db()
    with db.session() as s:
        s.add(_email_obligation(subject="will fail"))
        s.add(_email_obligation(subject="will succeed"))
        s.commit()

    # Cycle two responses: first invalid JSON, second valid
    bad_lm = StubLanguageModel(
        responses=[
            "not valid json {{",
            _json.dumps({"action": "flag", "score": 0.9, "reasoning": "ok"}),
        ]
    )
    report = triage_inbox(db=db, settings=AgentSettings(), language_model=bad_lm)

    assert report.triaged == 1  # only one succeeded
    assert len(report.errors) == 1


def test_triage_no_email_obligations_skipped() -> None:
    """Manual-source obligations are NOT triaged — only inbound_email."""
    db = _db()
    with db.session() as s:
        s.add(
            Obligation(
                title="manually created",
                source=ObligationSource.manual,
                status=ObligationStatus.inbox,
            )
        )
        s.commit()

    report = triage_inbox(db=db, settings=AgentSettings(), language_model=_triage_lm("flag"))
    assert report.candidates == 0


def test_run_tick_includes_triage_in_report() -> None:
    """End-to-end: run_tick wires through to triage_inbox + reports counts."""
    db = _db()
    with db.session() as s:
        s.add(_email_obligation())
        s.commit()

    report = run_tick(
        db=db,
        settings=AgentSettings(),
        language_model=_triage_lm("flag", score=0.9),
    )
    assert report.triage is not None
    assert report.triage.triaged == 1
    assert report.triage.by_action == {"flag": 1}


def test_run_tick_triage_disabled_skips_step() -> None:
    db = _db()
    with db.session() as s:
        s.add(_email_obligation())
        s.commit()

    report = run_tick(
        db=db,
        settings=AgentSettings(),
        language_model=_triage_lm("flag"),
        triage_enabled=False,
    )
    assert report.triage is None


# ── Sprint 20: digest delivery wiring ──────────────────────────────────────


def test_run_tick_attempts_digest_delivery_when_dispatcher_present():
    """A tick with non-empty content should fire the digest path."""
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=30))
        s.commit()

    transport = _RecordingTransport()
    dispatcher = NotificationDispatcher(transport, enabled=True, urgency_floor=Urgency.info)
    report = run_tick(db=db, settings=AgentSettings(), dispatcher=dispatcher)

    assert report.digest_delivery_attempted
    assert report.digest_delivery_sent
    assert report.digest_delivery_reason == "sent"
    # Two transport calls: stalled-notification + digest
    titles = [c["title"] for c in transport.calls]
    assert any("Stalled" in t for t in titles)
    assert any("Daily digest" in t for t in titles)


def test_run_tick_digest_delivery_disabled_skips_path():
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=30))
        s.commit()
    transport = _RecordingTransport()
    dispatcher = NotificationDispatcher(transport, enabled=True, urgency_floor=Urgency.info)
    report = run_tick(
        db=db,
        settings=AgentSettings(),
        dispatcher=dispatcher,
        digest_delivery_enabled=False,
    )
    assert not report.digest_delivery_attempted
    assert report.digest_delivery_reason is None


def test_run_tick_digest_skipped_when_dispatcher_none():
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=30))
        s.commit()
    report = run_tick(db=db, settings=AgentSettings(), dispatcher=None)
    assert not report.digest_delivery_attempted


def test_run_tick_second_tick_cadence_gates_digest():
    """First tick fires digest; second within period should be too-recent."""
    db = _db()
    with db.session() as s:
        s.add(_stalled_obligation(hours_old=30))
        s.commit()

    transport = _RecordingTransport()
    dispatcher = NotificationDispatcher(transport, enabled=True, urgency_floor=Urgency.info)
    settings = AgentSettings()

    r1 = run_tick(db=db, settings=settings, dispatcher=dispatcher)
    assert r1.digest_delivery_sent

    r2 = run_tick(db=db, settings=settings, dispatcher=dispatcher)
    assert not r2.digest_delivery_sent
    assert r2.digest_delivery_reason == "skipped_too_recent"
    # Only one digest in transport calls (plus the first stalled-notification;
    # second tick is silent because incident already open).
    digest_calls = [c for c in transport.calls if "Daily digest" in c["title"]]
    assert len(digest_calls) == 1


def test_run_tick_digest_skipped_empty_when_no_activity():
    """Empty db → digest delivery returns skipped_empty."""
    db = _db()
    transport = _RecordingTransport()
    dispatcher = NotificationDispatcher(transport, enabled=True, urgency_floor=Urgency.info)
    report = run_tick(db=db, settings=AgentSettings(), dispatcher=dispatcher)
    assert report.digest_delivery_attempted
    assert not report.digest_delivery_sent
    assert report.digest_delivery_reason == "skipped_empty"


# ── Sprint 21: email fetch wiring ──────────────────────────────────────────


def test_run_tick_skips_email_fetch_when_imap_disabled():
    """Default settings have email.imap.enabled=False — tick should not
    even try to construct an EmailFetcher."""
    db = _db()
    settings = AgentSettings()
    assert settings.email.imap.enabled is False
    report = run_tick(db=db, settings=settings, digest_delivery_enabled=False)
    assert report.email_fetched == 0
    assert report.email_captured == 0
    assert report.errors == []


def test_run_tick_records_email_fetch_error_when_misconfigured(monkeypatch):
    """email.imap.enabled=True but missing host/password → EmailFetchError
    surfaced in errors, no crash."""
    db = _db()
    settings = AgentSettings()
    settings.email.imap.enabled = True  # but host is empty

    report = run_tick(
        db=db,
        settings=settings,
        digest_delivery_enabled=False,
    )
    assert report.email_fetched == 0
    assert any("email fetch skipped" in e for e in report.errors)


def test_run_tick_calls_fetch_and_capture_when_enabled(monkeypatch):
    """When email is configured + secrets present, run_tick fires the
    fetcher and the counts make it into TickReport."""
    db = _db()
    settings = AgentSettings()
    settings.email.imap.enabled = True
    settings.email.imap.host = "imap.example.com"
    settings.email.imap.username = "u@example.com"

    # Stub the secrets store
    class _Secrets:
        def get(self, ns, key):
            return "secret-pw" if (ns, key) == ("email", "imap_password") else None

    monkeypatch.setattr("agent_core.secrets.default_store", lambda: _Secrets())

    # Stub fetch_and_capture so we don't actually network
    captured_args = {}

    def _fake_fetch_and_capture(*, fetcher, db, limit):
        captured_args["fetcher"] = fetcher
        captured_args["limit"] = limit
        from agent_core.work.email_fetch import FetchReport

        return FetchReport(fetched=3, captured=2, skipped_duplicate=1, errors=[])

    monkeypatch.setattr("agent_core.work.email_fetch.fetch_and_capture", _fake_fetch_and_capture)

    report = run_tick(
        db=db,
        settings=settings,
        digest_delivery_enabled=False,
    )
    assert report.email_fetched == 3
    assert report.email_captured == 2
    assert report.email_skipped_duplicate == 1
    assert captured_args["fetcher"].host == "imap.example.com"


def test_run_tick_email_fetch_disabled_via_kwarg(monkeypatch):
    """Even with imap.enabled=True, the kwarg lets callers (tests, etc)
    skip the fetch step explicitly."""
    db = _db()
    settings = AgentSettings()
    settings.email.imap.enabled = True
    settings.email.imap.host = "imap.example.com"
    settings.email.imap.username = "u@example.com"

    class _Secrets:
        def get(self, ns, key):
            return "secret-pw"

    monkeypatch.setattr("agent_core.secrets.default_store", lambda: _Secrets())
    # If our shim were called, it'd succeed — so we can verify it WASN'T
    # called by counting.
    call_count = {"n": 0}

    def _fake_fetch_and_capture(*, fetcher, db, limit):
        call_count["n"] += 1
        from agent_core.work.email_fetch import FetchReport

        return FetchReport()

    monkeypatch.setattr("agent_core.work.email_fetch.fetch_and_capture", _fake_fetch_and_capture)
    run_tick(
        db=db,
        settings=settings,
        digest_delivery_enabled=False,
        email_fetch_enabled=False,
    )
    assert call_count["n"] == 0
