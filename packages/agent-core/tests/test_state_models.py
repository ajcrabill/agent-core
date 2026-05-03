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
    CorrectionCandidate,
    CorrectionCandidateStatus,
    Identity,
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
    "identity",
    "peer",
    "obligation",
    "obligation_event",
    "plan",
    "completion_check",
    "learning_rule",
    "rule_firing",
    "correction_candidate",
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
