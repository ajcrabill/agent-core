"""Sprint 4.5 — action policy + daily digest tests."""

from __future__ import annotations

from datetime import timedelta

from agent_core.actions import (
    ActionPolicy,
    DailyDigestBuilder,
    PolicyKind,
)
from agent_core.state import (
    ActionClass,
    ActionLog,
    ActionOutcome,
    Database,
    Identity,
    Incident,
    IncidentSeverity,
    Obligation,
    ObligationStatus,
    utcnow,
)


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


# ── ActionPolicy defaults (L10) ──────────────────────────────────────────────


def test_default_autonomous_actions() -> None:
    p = ActionPolicy()
    for ac in (
        ActionClass.read,
        ActionClass.write_internal,
        ActionClass.ob_update,
        ActionClass.cross_agent_message,
        ActionClass.calendar_read,
        ActionClass.ingest,
        ActionClass.capture_learning_candidate,
    ):
        assert p.is_autonomous(ac), f"{ac} should be autonomous by default"


def test_default_gated_actions() -> None:
    p = ActionPolicy()
    for ac in (
        ActionClass.send_email_external,
        ActionClass.content_publish,
        ActionClass.calendar_invite_external,
        ActionClass.modify_people_data,
        ActionClass.install_skill,
    ):
        assert p.is_gated(ac), f"{ac} should be gated by default"


def test_default_forbidden_actions() -> None:
    p = ActionPolicy()
    assert p.is_forbidden(ActionClass.secret_access)
    assert p.is_forbidden(ActionClass.finance)


def test_decide_returns_kind_and_reason() -> None:
    p = ActionPolicy()
    d = p.decide(ActionClass.send_email_external)
    assert d.kind == PolicyKind.gated
    assert d.is_gated
    assert "human confirmation" in d.reason


# ── Overrides ────────────────────────────────────────────────────────────────


def test_constructor_overrides_apply() -> None:
    p = ActionPolicy(overrides={ActionClass.send_email_external: PolicyKind.autonomous})
    assert p.is_autonomous(ActionClass.send_email_external)


def test_set_changes_policy_for_one_class() -> None:
    p = ActionPolicy()
    assert p.is_gated(ActionClass.modify_people_data)
    p.set(ActionClass.modify_people_data, PolicyKind.autonomous)
    assert p.is_autonomous(ActionClass.modify_people_data)


def test_reset_to_default_restores_l10_value() -> None:
    p = ActionPolicy()
    p.set(ActionClass.send_email_external, PolicyKind.autonomous)
    p.reset_to_default(ActionClass.send_email_external)
    assert p.is_gated(ActionClass.send_email_external)


def test_serialization_roundtrip() -> None:
    p = ActionPolicy(overrides={ActionClass.send_email_external: PolicyKind.autonomous})
    snapshot = p.as_dict()
    assert snapshot["send_email_external"] == "autonomous"
    p2 = ActionPolicy.from_dict(snapshot)
    assert p2.is_autonomous(ActionClass.send_email_external)
    # Overrides preserved; defaults still apply for non-overridden
    assert p2.is_forbidden(ActionClass.finance)


def test_from_dict_tolerates_unknown_enum_values() -> None:
    """Forward-compat: a future ActionClass not in this version's enum
    shouldn't crash from_dict."""
    snapshot = {
        "send_email_external": "autonomous",
        "future_unseen_class": "gated",  # not in enum
    }
    p = ActionPolicy.from_dict(snapshot)
    assert p.is_autonomous(ActionClass.send_email_external)


# ── DailyDigest ──────────────────────────────────────────────────────────────


def test_digest_empty_when_no_activity() -> None:
    digest = DailyDigestBuilder(_empty_db()).build()
    assert digest.actions_total == 0
    assert digest.closed_obligations == []
    md = digest.as_markdown()
    assert "Nothing to report" in md


def test_digest_uses_instance_name_when_present() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Identity(instance_name="Loriah"))
        s.commit()
    digest = DailyDigestBuilder(db).build()
    assert digest.instance_name == "Loriah"
    assert "Daily digest from Loriah" in digest.as_markdown()


def test_digest_falls_back_to_generic_name_without_identity() -> None:
    digest = DailyDigestBuilder(_empty_db()).build()
    assert "your agent" in digest.as_markdown()


def test_digest_counts_actions_by_outcome() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t")
        s.add(ob)
        s.commit()
        ob_id = ob.id
        for outcome in (
            ActionOutcome.succeeded,
            ActionOutcome.succeeded,
            ActionOutcome.failed,
            ActionOutcome.blocked_by_policy,
            ActionOutcome.deferred,
        ):
            s.add(
                ActionLog(
                    obligation_id=ob_id,
                    action_class=ActionClass.read,
                    outcome=outcome,
                )
            )
        s.commit()

    digest = DailyDigestBuilder(db).build()
    assert digest.actions_total == 5
    assert digest.actions_succeeded == 2
    assert digest.actions_failed == 1
    assert digest.actions_blocked_by_policy == 1
    assert digest.actions_deferred == 1


def test_digest_breaks_down_by_action_class() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t")
        s.add(ob)
        s.commit()
        ob_id = ob.id
        s.add(
            ActionLog(
                obligation_id=ob_id,
                action_class=ActionClass.read,
                outcome=ActionOutcome.succeeded,
            )
        )
        s.add(
            ActionLog(
                obligation_id=ob_id,
                action_class=ActionClass.read,
                outcome=ActionOutcome.succeeded,
            )
        )
        s.add(
            ActionLog(
                obligation_id=ob_id,
                action_class=ActionClass.write_internal,
                outcome=ActionOutcome.succeeded,
            )
        )
        s.commit()
    digest = DailyDigestBuilder(db).build()
    assert digest.actions_by_class == {"read": 2, "write_internal": 1}


def test_digest_lists_closed_obligations() -> None:
    db = _empty_db()
    now = utcnow()
    with db.session() as s:
        s.add(
            Obligation(
                title="finished thing",
                status=ObligationStatus.done,
                completed_at=now - timedelta(hours=2),
            )
        )
        s.commit()
    digest = DailyDigestBuilder(db).build()
    assert len(digest.closed_obligations) == 1
    assert digest.closed_obligations[0]["title"] == "finished thing"


def test_digest_excludes_obligations_closed_outside_window() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(
            Obligation(
                title="ancient closure",
                status=ObligationStatus.done,
                completed_at=utcnow() - timedelta(days=10),
            )
        )
        s.commit()
    digest = DailyDigestBuilder(db).build()
    assert digest.closed_obligations == []


def test_digest_lists_failed_actions_with_error_and_obligation() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t")
        s.add(ob)
        s.commit()
        ob_id = ob.id
        s.add(
            ActionLog(
                obligation_id=ob_id,
                action_class=ActionClass.send_email_external,
                outcome=ActionOutcome.failed,
                target="x@example.com",
                error="connection refused",
                rationale="step 2 of plan to reach charlotte",
            )
        )
        s.commit()
    digest = DailyDigestBuilder(db).build()
    assert len(digest.failed_actions) == 1
    f = digest.failed_actions[0]
    assert f["error"] == "connection refused"
    assert f["target"] == "x@example.com"
    assert f["obligation_id"] == ob_id


def test_digest_highlights_external_facing_actions() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t")
        s.add(ob)
        s.commit()
        ob_id = ob.id
        s.add(
            ActionLog(
                obligation_id=ob_id,
                action_class=ActionClass.send_email_external,
                outcome=ActionOutcome.succeeded,
                target="charlotte@example.com",
            )
        )
        s.add(
            ActionLog(
                obligation_id=ob_id,
                action_class=ActionClass.read,  # internal — should NOT appear in notable_external
                outcome=ActionOutcome.succeeded,
            )
        )
        s.commit()
    digest = DailyDigestBuilder(db).build()
    assert len(digest.notable_external) == 1
    assert digest.notable_external[0]["action_class"] == "send_email_external"


def test_digest_carries_open_incidents() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(
            Incident(
                title="Gmail OAuth refresh failed",
                severity=IncidentSeverity.high,
                source="cron",
            )
        )
        s.commit()
    digest = DailyDigestBuilder(db).build()
    assert len(digest.open_incidents) == 1
    assert digest.open_incidents[0]["title"] == "Gmail OAuth refresh failed"


def test_digest_excludes_actions_outside_window() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t")
        s.add(ob)
        s.commit()
        ob_id = ob.id
        # 30 days ago
        a = ActionLog(
            obligation_id=ob_id,
            action_class=ActionClass.read,
            outcome=ActionOutcome.succeeded,
            occurred_at=utcnow() - timedelta(days=30),
        )
        s.add(a)
        s.commit()
    digest = DailyDigestBuilder(db).build()
    assert digest.actions_total == 0


def test_digest_period_hours_is_configurable() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t")
        s.add(ob)
        s.commit()
        ob_id = ob.id
        # Action 5 hours ago
        s.add(
            ActionLog(
                obligation_id=ob_id,
                action_class=ActionClass.read,
                outcome=ActionOutcome.succeeded,
                occurred_at=utcnow() - timedelta(hours=5),
            )
        )
        s.commit()
    # 1-hour window should miss it
    digest_1h = DailyDigestBuilder(db, period_hours=1).build()
    assert digest_1h.actions_total == 0
    # 24-hour window should catch it
    digest_24h = DailyDigestBuilder(db, period_hours=24).build()
    assert digest_24h.actions_total == 1


def test_digest_markdown_renders_all_sections() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Identity(instance_name="Loriah"))
        ob = Obligation(
            title="closed task",
            status=ObligationStatus.done,
            completed_at=utcnow(),
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id

        s.add(
            ActionLog(
                obligation_id=ob_id,
                action_class=ActionClass.send_email_external,
                outcome=ActionOutcome.succeeded,
                target="x@y",
                rationale="reach out to charlotte",
            )
        )
        s.add(
            ActionLog(
                obligation_id=ob_id,
                action_class=ActionClass.write_internal,
                outcome=ActionOutcome.failed,
                error="permission denied",
            )
        )
        s.add(Incident(title="Stale token", source="cron"))
        s.commit()

    md = DailyDigestBuilder(db).build().as_markdown()
    for needle in (
        "Daily digest from Loriah",
        "Closed obligations",
        "closed task",
        "Failures",
        "permission denied",
        "External-facing actions",
        "send_email_external",
        "By action class",
        "Open incidents",
        "Stale token",
    ):
        assert needle in md, f"missing in digest: {needle!r}"
