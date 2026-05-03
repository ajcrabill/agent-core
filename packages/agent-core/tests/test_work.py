"""Sprint 3 tests — inbound capture, pipeline monitor, incident recorder."""

from __future__ import annotations

from datetime import timedelta

import pytest
from agent_core.state import (
    Database,
    Identity,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    Obligation,
    ObligationEvent,
    ObligationEventKind,
    ObligationOwner,
    ObligationSource,
    ObligationStatus,
    utcnow,
)
from agent_core.work import (
    InboundCapture,
    IncidentRecorder,
    PipelineMonitor,
)
from sqlmodel import select


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


# ── Inbound capture ─────────────────────────────────────────────────────────


def test_capture_email_creates_obligation_with_correct_source() -> None:
    db = _empty_db()
    cap = InboundCapture(db)
    ob = cap.capture_email(
        sender="charlotte@example.com",
        subject="dinner Friday?",
        body="Want to grab dinner this week?",
    )
    assert ob.source == ObligationSource.inbound_email
    assert ob.status == ObligationStatus.inbox
    assert ob.owner == ObligationOwner.agent
    assert "charlotte@example.com" in ob.title
    assert "dinner" in ob.title


def test_capture_email_writes_created_event_with_metadata() -> None:
    db = _empty_db()
    InboundCapture(db).capture_email(
        sender="x@y",
        subject="s",
        body="b",
        message_id="msg-123",
        thread_id="thr-9",
    )
    with db.session() as s:
        ev = s.exec(select(ObligationEvent)).first()
    assert ev.kind == ObligationEventKind.created
    assert ev.payload["sender"] == "x@y"
    assert ev.payload["message_id"] == "msg-123"
    assert ev.payload["thread_id"] == "thr-9"
    assert ev.payload["kind"] == "email"


def test_capture_email_default_criteria_is_principal_ratification() -> None:
    """Email obligations need explicit human OK to close (per L20)."""
    ob = InboundCapture(_empty_db()).capture_email(sender="x", subject="y", body="z")
    assert ob.completion_criteria == [{"type": "principal_ratification"}]


def test_capture_email_truncates_long_subject() -> None:
    long_subject = "x" * 500
    ob = InboundCapture(_empty_db()).capture_email(sender="a", subject=long_subject, body="")
    assert len(ob.title) <= 200


def test_capture_chat_uses_principal_chat_source() -> None:
    db = _empty_db()
    ob = InboundCapture(db).capture_chat(text="please reply to charlotte")
    assert ob.source == ObligationSource.principal_chat
    assert "please reply to charlotte" in ob.title or "please reply to charlotte" in (ob.body or "")


def test_capture_chat_accepts_explicit_completion_criteria() -> None:
    """The chat layer can pre-fill richer criteria than the default."""
    custom = [
        {"type": "email_sent", "to": "charlotte@example.com"},
        {"type": "principal_ratification"},
    ]
    ob = InboundCapture(_empty_db()).capture_chat(
        text="reply to charlotte",
        suggested_completion_criteria=custom,
    )
    assert ob.completion_criteria == custom


def test_capture_peer_message_uses_peer_message_source() -> None:
    db = _empty_db()
    ob = InboundCapture(db).capture_peer_message(
        sender="Esby",
        body="Q3 metrics ready",
        intercom_message_id="inter-42",
    )
    assert ob.source == ObligationSource.peer_message
    assert "Esby" in ob.title
    # Default criterion is peer_acknowledged with the intercom id
    assert ob.completion_criteria == [
        {"type": "peer_acknowledged", "intercom_message_id": "inter-42"}
    ]


def test_capture_cron_uses_cron_trigger_source() -> None:
    ob = InboundCapture(_empty_db()).capture_cron(
        job_name="morning-briefing",
        title="Generate morning briefing",
        completion_criteria=[{"type": "principal_ratification"}],
    )
    assert ob.source == ObligationSource.cron_trigger
    assert ob.title == "Generate morning briefing"


def test_capture_subtask_links_parent() -> None:
    db = _empty_db()
    cap = InboundCapture(db)
    parent = cap.capture_chat(text="big plan")
    sub = cap.capture_subtask(
        parent_id=parent.id,
        title="step 1",
        completion_criteria=[{"type": "principal_ratification"}],
    )
    assert sub.parent_id == parent.id
    assert sub.source == ObligationSource.agent_decomposition


def test_capture_uses_identity_instance_name_as_actor() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Identity(instance_name="Loriah"))
        s.commit()
    InboundCapture(db).capture_chat(text="hello")
    with db.session() as s:
        ev = s.exec(select(ObligationEvent)).first()
    assert ev.actor == "Loriah"


def test_capture_falls_back_to_generic_actor_without_identity() -> None:
    db = _empty_db()
    InboundCapture(db).capture_chat(text="hello")
    with db.session() as s:
        ev = s.exec(select(ObligationEvent)).first()
    assert ev.actor == "agent"


# ── Pipeline monitor ────────────────────────────────────────────────────────


def _stale(db: Database, ob_id: str, hours_ago: float) -> None:
    """Force ``updated_at`` to be hours_ago hours in the past."""
    past = utcnow() - timedelta(hours=hours_ago)
    with db.session() as s:
        ob = s.get(Obligation, ob_id)
        ob.updated_at = past
        s.add(ob)
        s.commit()


def test_pipeline_monitor_finds_stalled_in_progress() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="fresh", status=ObligationStatus.in_progress))
        ob = Obligation(title="stale", status=ObligationStatus.in_progress)
        s.add(ob)
        s.commit()
        stale_id = ob.id
    _stale(db, stale_id, hours_ago=48)  # > 24h default

    stalled = PipelineMonitor(db).find_stalled()
    titles = [s.obligation.title for s in stalled]
    assert "stale" in titles
    assert "fresh" not in titles


def test_pipeline_monitor_finds_stalled_waiting() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="long-waiting", status=ObligationStatus.waiting)
        s.add(ob)
        s.commit()
        ob_id = ob.id
    _stale(db, ob_id, hours_ago=200)  # > 168h default
    stalled = PipelineMonitor(db).find_stalled()
    assert any(s.reason == "waiting_too_long" for s in stalled)


def test_pipeline_monitor_skips_done() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="done one", status=ObligationStatus.done)
        s.add(ob)
        s.commit()
        ob_id = ob.id
    _stale(db, ob_id, hours_ago=1000)
    assert PipelineMonitor(db).find_stalled() == []


def test_pipeline_monitor_skips_principal_owned() -> None:
    """Stalled detection only applies to agent-owned obligations — if the
    principal owns it, the agent isn't responsible for moving it."""
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(
            title="principal task",
            status=ObligationStatus.in_progress,
            owner=ObligationOwner.principal,
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id
    _stale(db, ob_id, hours_ago=1000)
    assert PipelineMonitor(db).find_stalled() == []


def test_pipeline_monitor_detects_past_due_in_any_status() -> None:
    db = _empty_db()
    past = utcnow() - timedelta(hours=10)
    with db.session() as s:
        s.add(
            Obligation(
                title="overdue inbox",
                status=ObligationStatus.inbox,
                due_at=past,
            )
        )
        s.commit()
    stalled = PipelineMonitor(db).find_stalled()
    assert any(s.reason == "past_due" for s in stalled)


def test_pipeline_monitor_past_due_outranks_waiting_too_long() -> None:
    """When an obligation is both past_due AND waiting_too_long, the more
    urgent reason (past_due) is the one surfaced."""
    db = _empty_db()
    past = utcnow() - timedelta(hours=200)
    with db.session() as s:
        ob = Obligation(
            title="both",
            status=ObligationStatus.waiting,
            due_at=past,
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id
    _stale(db, ob_id, hours_ago=200)
    stalled = PipelineMonitor(db).find_stalled()
    assert len(stalled) == 1
    assert stalled[0].reason == "past_due"


def test_scan_and_record_creates_incidents() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="stale", status=ObligationStatus.in_progress)
        s.add(ob)
        s.commit()
        ob_id = ob.id
    _stale(db, ob_id, hours_ago=48)

    pm = PipelineMonitor(db)
    result = pm.scan_and_record()
    assert result.incidents_created == 1

    with db.session() as s:
        inc = s.exec(select(Incident)).first()
    assert inc.related_obligation_id == ob_id
    assert inc.source == "pipeline_monitor"


def test_scan_and_record_dedups_open_incidents() -> None:
    """Re-scanning when the same obligation is still stalled doesn't create
    a second incident."""
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="stale", status=ObligationStatus.in_progress)
        s.add(ob)
        s.commit()
        ob_id = ob.id
    _stale(db, ob_id, hours_ago=48)

    pm = PipelineMonitor(db)
    first = pm.scan_and_record()
    second = pm.scan_and_record()
    assert first.incidents_created == 1
    assert second.incidents_created == 0
    assert second.incidents_already_open == 1
    with db.session() as s:
        assert len(list(s.exec(select(Incident)).all())) == 1


def test_severity_escalates_with_age() -> None:
    db = _empty_db()
    with db.session() as s:
        # 2 days past due → medium
        s.add(
            Obligation(
                title="med",
                status=ObligationStatus.in_progress,
                due_at=utcnow() - timedelta(days=2),
            )
        )
        # 10 days past due → high
        s.add(
            Obligation(
                title="high",
                status=ObligationStatus.in_progress,
                due_at=utcnow() - timedelta(days=10),
            )
        )
        s.commit()
    PipelineMonitor(db).scan_and_record()
    with db.session() as s:
        sev_by_title = {
            (i.related_obligation_id, i.severity): i.title for i in s.exec(select(Incident)).all()
        }
    severities = sorted({s for (_, s) in sev_by_title})
    assert IncidentSeverity.high in severities
    assert IncidentSeverity.medium in severities


# ── Incident recorder ───────────────────────────────────────────────────────


def test_incident_recorder_creates_incident() -> None:
    db = _empty_db()
    rec = IncidentRecorder(db)
    inc = rec.record(
        title="Tool call failed",
        source="tool_call",
        severity=IncidentSeverity.medium,
        payload={"tool": "send_email", "error": "auth"},
    )
    assert inc.id is not None
    assert inc.status == IncidentStatus.open
    assert inc.payload["tool"] == "send_email"


def test_incident_recorder_dedups_open_by_default() -> None:
    db = _empty_db()
    rec = IncidentRecorder(db)
    a = rec.record(title="X", source="cron")
    b = rec.record(title="X", source="cron")
    assert a.id == b.id
    with db.session() as s:
        assert len(list(s.exec(select(Incident)).all())) == 1


def test_incident_recorder_dedup_disabled_creates_duplicates() -> None:
    db = _empty_db()
    rec = IncidentRecorder(db)
    a = rec.record(title="X", source="cron", dedup_open=False)
    b = rec.record(title="X", source="cron", dedup_open=False)
    assert a.id != b.id


def test_incident_recorder_dedup_per_obligation() -> None:
    """Same title+source for DIFFERENT obligations are NOT dedup'd."""
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="ob1"))
        s.add(Obligation(title="ob2"))
        s.commit()
        obs = list(s.exec(select(Obligation)).all())
    rec = IncidentRecorder(db)
    a = rec.record(title="failed", source="tool_call", related_obligation_id=obs[0].id)
    b = rec.record(title="failed", source="tool_call", related_obligation_id=obs[1].id)
    assert a.id != b.id


def test_incident_acknowledge_transitions_status() -> None:
    db = _empty_db()
    rec = IncidentRecorder(db)
    inc = rec.record(title="t", source="s")
    ack = rec.acknowledge(inc.id)
    assert ack.status == IncidentStatus.acknowledged
    assert ack.acknowledged_at is not None


def test_incident_resolve_transitions_status_and_appends_note() -> None:
    db = _empty_db()
    rec = IncidentRecorder(db)
    inc = rec.record(title="t", source="s")
    resolved = rec.resolve(inc.id, note="fixed by retrying")
    assert resolved.status == IncidentStatus.resolved
    assert resolved.resolved_at is not None
    assert "fixed by retrying" in resolved.payload["resolution_notes"]


def test_resolved_incident_can_be_recorded_again() -> None:
    """After resolution, a fresh incident with the same title+source is a
    NEW incident — dedup only matches open/acknowledged."""
    db = _empty_db()
    rec = IncidentRecorder(db)
    a = rec.record(title="X", source="cron")
    rec.resolve(a.id)
    b = rec.record(title="X", source="cron")
    assert a.id != b.id


def test_open_for_obligation_returns_only_open_or_acknowledged() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t")
        s.add(ob)
        s.commit()
        ob_id = ob.id
    rec = IncidentRecorder(db)
    a = rec.record(title="A", source="s", related_obligation_id=ob_id)
    b = rec.record(title="B", source="s", related_obligation_id=ob_id)
    rec.resolve(a.id)
    open_incs = rec.open_for_obligation(ob_id)
    ids = {i.id for i in open_incs}
    assert b.id in ids
    assert a.id not in ids


def test_has_open_for_obligation() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t")
        s.add(ob)
        s.commit()
        ob_id = ob.id
    rec = IncidentRecorder(db)
    assert rec.has_open_for_obligation(ob_id) is False
    rec.record(title="x", source="s", related_obligation_id=ob_id)
    assert rec.has_open_for_obligation(ob_id) is True


def test_acknowledge_unknown_id_raises() -> None:
    rec = IncidentRecorder(_empty_db())
    with pytest.raises(ValueError, match="not found"):
        rec.acknowledge("nope")


def test_resolve_unknown_id_raises() -> None:
    rec = IncidentRecorder(_empty_db())
    with pytest.raises(ValueError, match="not found"):
        rec.resolve("nope")


# ── End-to-end: capture → loop-ready ────────────────────────────────────────


def test_captured_obligation_appears_in_context_loader_block() -> None:
    """End-to-end smoke: capture an inbound, then verify the context loader
    surfaces it in its obligations block."""
    from agent_core.agent import ContextLoader

    db = _empty_db()
    InboundCapture(db).capture_email(sender="charlotte@x", subject="dinner?", body="...")
    bundle = ContextLoader(db).collect()
    obs = bundle.by_name("obligations")
    assert obs is not None
    assert not obs.is_empty
    assert "dinner" in obs.content.lower() or "charlotte" in obs.content.lower()
