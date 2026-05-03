"""Schema smoke tests for agent_core.state.models.

Verifies:
  - All declared models register on SQLModel.metadata
  - Schema compiles cleanly to SQLite (in-memory)
  - Schema compiles cleanly to Postgres dialect (DDL generation only)
  - No SAWarnings during create_all (e.g., unresolvable FK cycles)
  - Enum columns use VARCHAR (not native Postgres ENUM)
  - Basic insert + roundtrip for the most-used tables
"""

from __future__ import annotations

import warnings

import pytest
from agent_core.state import (
    ActionClass,
    ActionLog,
    ActionOutcome,
    Calibration,
    CorrectionCandidate,
    CorrectionCandidateStatus,
    Exemplar,
    Identity,
    Incident,
    IncidentStatus,
    IntercomMessage,
    IntercomState,
    Iteration,
    IterationStatus,
    LearningRule,
    Obligation,
    ObligationEvent,
    ObligationEventKind,
    ObligationStatus,
    Plan,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlmodel import Session, SQLModel, create_engine, select

EXPECTED_TABLES = {
    # Identity
    "identity",
    "peer",
    # Work
    "obligation",
    "obligation_event",
    "plan",
    "completion_check",
    # Learning
    "learning_rule",
    "rule_firing",
    "correction_candidate",
    # Delegations
    "delegation",
    # Run / Incidents / Actions
    "run_log",
    "incident",
    "action_log",
    # Quality
    "quality_audit",
    "quality_score",
    # Mesh
    "intercom_message",
    "intercom_ack",
    # Sessions / Metrics
    "session",
    "metric",
    # Content creation
    "exemplar",
    "iteration",
    "template",
    "calibration",
    # OpenBrain
    "thought",
    "thought_source",
    "ingestion_run",
}


def test_all_expected_tables_registered() -> None:
    actual = set(SQLModel.metadata.tables.keys())
    missing = EXPECTED_TABLES - actual
    assert not missing, f"missing tables: {missing}"


def test_sqlite_create_all_no_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        engine = create_engine("sqlite:///:memory:", echo=False)
        SQLModel.metadata.create_all(engine)


def test_postgres_ddl_compiles() -> None:
    """Generate Postgres DDL for every table without a connection.

    Catches dialect-specific compilation failures early.
    """
    for table in SQLModel.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE" in ddl


def test_enum_columns_use_varchar_not_native_enum() -> None:
    """Locked design choice: enums stored as VARCHAR for cross-backend portability.

    Native PG ENUM types complicate migrations and don't exist on SQLite.
    """
    obligation = SQLModel.metadata.tables["obligation"]
    pg_ddl = str(CreateTable(obligation).compile(dialect=postgresql.dialect()))
    # If we accidentally regress to native PG enums, the type name appears
    # before the column declaration (e.g., "status obligationstatus NOT NULL").
    for col in ("status", "owner", "source"):
        # Find the column line
        line = next(line for line in pg_ddl.split("\n") if line.strip().startswith(col + " "))
        assert "VARCHAR" in line, f"{col} should be VARCHAR, not native enum: {line!r}"


def test_obligation_plan_fk_cycle_resolved() -> None:
    """Guard against re-introducing the obligation.plan_id FK that creates a
    cycle with plan.obligation_id."""
    obligation = SQLModel.metadata.tables["obligation"]
    assert "plan_id" not in obligation.c, (
        "obligation.plan_id was removed; the active plan is derived via "
        "SELECT * FROM plan WHERE obligation_id=? ORDER BY created_at DESC LIMIT 1"
    )


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_identity_insert_roundtrip(session: Session) -> None:
    me = Identity(instance_name="TestAgent", persona_email="t@e.com")
    session.add(me)
    session.commit()
    assert session.get(Identity, "self").instance_name == "TestAgent"


def test_obligation_with_completion_criteria(session: Session) -> None:
    ob = Obligation(
        title="Reply to msg",
        completion_criteria=[
            {"type": "email_sent", "to": "x@y"},
            {"type": "principal_ratification"},
        ],
    )
    session.add(ob)
    session.commit()

    fetched = session.exec(select(Obligation)).first()
    assert fetched is not None
    assert fetched.status == ObligationStatus.inbox  # default
    assert len(fetched.completion_criteria) == 2
    assert fetched.completion_criteria[0]["type"] == "email_sent"


def test_plan_with_steps(session: Session) -> None:
    ob = Obligation(title="t")
    session.add(ob)
    session.commit()
    plan = Plan(
        obligation_id=ob.id,
        steps=[
            {"description": "Step 1", "action_class": "read", "expected_outcome": "data"},
            {"description": "Step 2", "action_class": "write_internal", "depends_on": [0]},
        ],
        confidence=0.7,
    )
    session.add(plan)
    session.commit()

    fetched = session.exec(select(Plan)).first()
    assert fetched is not None
    assert fetched.current_step == 0
    assert len(fetched.steps) == 2
    assert fetched.steps[1]["depends_on"] == [0]


def test_obligation_event_audit(session: Session) -> None:
    ob = Obligation(title="t")
    session.add(ob)
    session.commit()
    ev = ObligationEvent(
        obligation_id=ob.id,
        kind=ObligationEventKind.created,
        actor="agent",
        payload={"why": "test"},
    )
    session.add(ev)
    session.commit()
    assert session.exec(select(ObligationEvent)).first().kind == ObligationEventKind.created


def test_learning_rule_default_general_tag(session: Session) -> None:
    rule = LearningRule(
        correction="Be concise. No filler.",
        source="principal chat 2026-05-02",
    )
    session.add(rule)
    session.commit()

    fetched = session.exec(select(LearningRule)).first()
    assert fetched.skill_tags == ["general"]


def test_learning_rule_skill_scoped(session: Session) -> None:
    rule = LearningRule(
        correction="When drafting BD emails, lead with the prospect's recent post.",
        skill_tags=["email-composer"],
        source="principal correction",
    )
    session.add(rule)
    session.commit()

    fetched = session.exec(select(LearningRule)).first()
    assert fetched.skill_tags == ["email-composer"]


def test_correction_candidate_pending(session: Session) -> None:
    cc = CorrectionCandidate(
        detected_correction="Use 'their' not 'his/her'",
        inferred_skill_tags=["email-composer"],
        confidence=0.85,
        source_excerpt="actually use 'their' instead",
    )
    session.add(cc)
    session.commit()

    fetched = session.exec(select(CorrectionCandidate)).first()
    assert fetched.status == CorrectionCandidateStatus.pending
    assert fetched.confidence == 0.85


# ── Goal-directed-operation enforcement (L20) ────────────────────────────────


def test_action_log_requires_obligation_id() -> None:
    """Per L20, every autonomous action MUST trace to an obligation.
    `obligation_id` is non-nullable."""
    table = SQLModel.metadata.tables["action_log"]
    assert table.c.obligation_id.nullable is False, (
        "action_log.obligation_id must be NOT NULL per L20 — every action traces to an obligation"
    )


def test_action_log_with_obligation(session: Session) -> None:
    ob = Obligation(title="Send the thing")
    session.add(ob)
    session.commit()

    al = ActionLog(
        obligation_id=ob.id,
        action_class=ActionClass.send_email_external,
        target="x@example.com",
        rationale="Plan step 2 of obligation 'Send the thing'; rules: lr-024 (signature).",
        outcome=ActionOutcome.succeeded,
    )
    session.add(al)
    session.commit()

    fetched = session.exec(select(ActionLog)).first()
    assert fetched.obligation_id == ob.id
    assert fetched.action_class == ActionClass.send_email_external
    assert fetched.outcome == ActionOutcome.succeeded


def test_incident_default_open(session: Session) -> None:
    inc = Incident(title="Tool call failed", source="tool_call")
    session.add(inc)
    session.commit()
    assert session.exec(select(Incident)).first().status == IncidentStatus.open


# ── Mesh / Intercom ──────────────────────────────────────────────────────────


def test_intercom_message_default_pending(session: Session) -> None:
    msg = IntercomMessage(sender="loriah", recipient="esby", body="ping")
    session.add(msg)
    session.commit()
    fetched = session.exec(select(IntercomMessage)).first()
    assert fetched.state == IntercomState.pending
    assert fetched.ttl_seconds == 7 * 24 * 3600


# ── Content creation ─────────────────────────────────────────────────────────


def test_exemplar_default_not_synthetic(session: Session) -> None:
    """Exemplars from natural iterations have is_synthetic=False; only the
    L21 synthetic-battery sets True."""
    ex = Exemplar(skill="email-composer", content="Hi X, …", title="BD outreach #1")
    session.add(ex)
    session.commit()
    fetched = session.exec(select(Exemplar)).first()
    assert fetched.is_synthetic is False


def test_iteration_default_in_progress_and_natural(session: Session) -> None:
    it = Iteration(skill="document-creator", raw_input="rough notes here")
    session.add(it)
    session.commit()
    fetched = session.exec(select(Iteration)).first()
    assert fetched.status == IterationStatus.in_progress
    assert fetched.is_synthetic is False
    assert fetched.attempts == []
    assert fetched.corrections == []


def test_calibration_starts_with_human_review_required(session: Session) -> None:
    """New skills start with autonomous_mode=False; quality auditor flips it
    True only after threshold confidence + N consecutive ratifications."""
    cal = Calibration(skill="document-creator")
    session.add(cal)
    session.commit()
    fetched = session.exec(select(Calibration)).first()
    assert fetched.autonomous_mode is False
    assert fetched.confidence == 0.0
    assert fetched.autonomous_mode_threshold == 0.85


def test_learning_rule_supersede_chain(session: Session) -> None:
    """Older rules can be replaced by newer ones via superseded_by FK."""
    old = LearningRule(correction="Use periods.", source="old")
    session.add(old)
    session.commit()
    new = LearningRule(
        correction="Use periods. Avoid em-dashes.",
        source="newer correction",
        superseded_by=None,
    )
    session.add(new)
    session.commit()
    # Mark old as superseded by new
    old.superseded_by = new.id
    session.commit()

    fetched = session.exec(select(LearningRule).where(LearningRule.id == old.id)).first()
    assert fetched.superseded_by == new.id
