"""Agent loop + verifier tests.

The loop is dependency-injected with PlanDeveloper / StepExecutor mocks so we
can exercise every branch (plan / execute / verify-and-close /
verify-and-block) without needing Hermes.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from agent_core.agent import (
    AgentLoop,
    CheckResult,
    CompletionVerifier,
    ContextBundle,
    ContextLoader,
    PlanProposal,
    StepResult,
)
from agent_core.state import (
    ActionClass,
    ActionLog,
    ActionOutcome,
    Database,
    Obligation,
    ObligationEvent,
    ObligationEventKind,
    ObligationStatus,
    Plan,
    PlanStatus,
    utcnow,
)
from sqlmodel import select


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


# ── Mocks ────────────────────────────────────────────────────────────────────


class StubPlanDeveloper:
    """Always returns the same fixed plan."""

    def __init__(self, steps=None, confidence: float = 0.9, rationale: str = "") -> None:
        self.steps = (
            steps
            if steps is not None
            else [
                {"description": "step 0", "action_class": "read"},
                {"description": "step 1", "action_class": "write_internal"},
            ]
        )
        self.confidence = confidence
        self.rationale = rationale
        self.calls = 0

    def develop(self, obligation, context: ContextBundle) -> PlanProposal:
        self.calls += 1
        return PlanProposal(
            steps=list(self.steps),
            confidence=self.confidence,
            rationale=self.rationale,
        )


class StubStepExecutor:
    """Returns the same StepResult every time it's called."""

    def __init__(
        self,
        action_class: ActionClass = ActionClass.read,
        outcome: ActionOutcome = ActionOutcome.succeeded,
    ) -> None:
        self.action_class = action_class
        self.outcome = outcome
        self.calls: list[tuple[Plan, int]] = []

    def execute(self, plan: Plan, step_index: int, context: ContextBundle) -> StepResult:
        self.calls.append((plan, step_index))
        return StepResult(
            action_class=self.action_class,
            outcome=self.outcome,
            target=f"step:{step_index}",
            rationale="stub executor",
        )


def _loop(
    db: Database,
    *,
    plan_developer=None,
    step_executor=None,
    register_default_verifiers: bool = True,
) -> AgentLoop:
    return AgentLoop(
        db=db,
        context_loader=ContextLoader(db),
        plan_developer=plan_developer or StubPlanDeveloper(),
        step_executor=step_executor or StubStepExecutor(),
        completion_verifier=CompletionVerifier(db, register_defaults=register_default_verifiers),
    )


# ── Idle ─────────────────────────────────────────────────────────────────────


def test_tick_idle_when_no_obligations() -> None:
    db = _empty_db()
    loop = _loop(db)
    outcome = loop.tick()
    assert outcome.idle is True
    assert outcome.did_anything is False


def test_tick_idle_when_only_done_obligations() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="finished", status=ObligationStatus.done))
        s.commit()
    outcome = _loop(db).tick()
    assert outcome.idle is True


# ── Plan-developing branch ───────────────────────────────────────────────────


def test_first_tick_develops_plan_for_planless_obligation() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="t"))
        s.commit()
    dev = StubPlanDeveloper(steps=[{"description": "do x"}])
    outcome = _loop(db, plan_developer=dev).tick()
    assert outcome.plans_developed == 1
    assert dev.calls == 1
    # A Plan row was written
    with db.session() as s:
        plans = list(s.exec(select(Plan)).all())
    assert len(plans) == 1
    assert plans[0].steps == [{"description": "do x"}]


def test_developing_plan_promotes_inbox_to_in_progress() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t", status=ObligationStatus.inbox)
        s.add(ob)
        s.commit()
        ob_id = ob.id
    _loop(db).tick()
    with db.session() as s:
        ob = s.get(Obligation, ob_id)
    assert ob.status == ObligationStatus.in_progress
    assert ob.started_at is not None
    # Audit events written for both
    with db.session() as s:
        events = list(s.exec(select(ObligationEvent)).all())
    kinds = [e.kind for e in events]
    assert ObligationEventKind.plan_developed in kinds
    assert ObligationEventKind.status_changed in kinds


def test_plan_with_zero_steps_marks_blocked_immediately() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="t"))
        s.commit()
    dev = StubPlanDeveloper(steps=[])
    _loop(db, plan_developer=dev).tick()
    with db.session() as s:
        plans = list(s.exec(select(Plan)).all())
    assert plans[0].status == PlanStatus.blocked


# ── Execute-step branch ──────────────────────────────────────────────────────


def test_second_tick_executes_first_plan_step() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="t"))
        s.commit()
    executor = StubStepExecutor()
    loop = _loop(db, step_executor=executor)
    loop.tick()  # plan
    outcome = loop.tick()  # execute step 0
    assert outcome.steps_executed == 1
    assert len(executor.calls) == 1
    assert executor.calls[0][1] == 0  # step_index
    # ActionLog written; obligation_id MUST be set (L20)
    with db.session() as s:
        actions = list(s.exec(select(ActionLog)).all())
    assert len(actions) == 1
    assert actions[0].obligation_id is not None
    assert actions[0].action_class == ActionClass.read


def test_executing_step_advances_plan_pointer() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="t"))
        s.commit()
    loop = _loop(db, plan_developer=StubPlanDeveloper(steps=[{}, {}, {}]))
    loop.tick()  # plan
    loop.tick()  # step 0
    loop.tick()  # step 1
    with db.session() as s:
        plan = s.exec(select(Plan)).first()
    assert plan.current_step == 2


def test_action_log_records_obligation_id_for_every_action() -> None:
    """L20 invariant: every ActionLog row has an obligation_id."""
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="t"))
        s.commit()
    loop = _loop(db, plan_developer=StubPlanDeveloper(steps=[{}, {}]))
    loop.tick()  # plan
    loop.tick()  # step 0
    loop.tick()  # step 1
    with db.session() as s:
        for al in s.exec(select(ActionLog)).all():
            assert al.obligation_id is not None, "every action MUST trace to an obligation (L20)"


# ── Verify branch ────────────────────────────────────────────────────────────


def test_verify_branch_fails_obligation_with_no_criteria() -> None:
    """An obligation MUST have explicit criteria to ever close. Empty criteria
    cannot pass verification (per L20)."""
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="no criteria", completion_criteria=[]))
        s.commit()
    loop = _loop(db, plan_developer=StubPlanDeveloper(steps=[{}]))
    loop.tick()  # plan
    loop.tick()  # step 0
    outcome = loop.tick()  # verify (fails — no criteria)
    assert outcome.completion_checks_run == 1
    assert outcome.obligations_closed == 0
    assert outcome.obligations_blocked == 1


def test_verify_branch_closes_obligation_when_all_criteria_pass() -> None:
    db = _empty_db()
    with db.session() as s:
        # Self-passing criterion via `principal_ratification` with ratified=true
        ob = Obligation(
            title="will close",
            completion_criteria=[{"type": "principal_ratification", "ratified": True}],
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id

    loop = _loop(db, plan_developer=StubPlanDeveloper(steps=[{}]))
    loop.tick()  # plan
    loop.tick()  # step 0
    outcome = loop.tick()  # verify → closes

    assert outcome.obligations_closed == 1
    assert ob_id in outcome.closed_ids
    with db.session() as s:
        ob = s.get(Obligation, ob_id)
    assert ob.status == ObligationStatus.done
    assert ob.completed_at is not None


def test_verify_branch_blocks_obligation_when_criterion_fails() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(
            title="blocked",
            completion_criteria=[
                {"type": "principal_ratification"}  # ratified is missing → fails
            ],
        )
        s.add(ob)
        s.commit()
    loop = _loop(db, plan_developer=StubPlanDeveloper(steps=[{}]))
    loop.tick()
    loop.tick()
    outcome = loop.tick()
    assert outcome.obligations_blocked == 1
    with db.session() as s:
        plan = s.exec(select(Plan)).first()
    assert plan.status == PlanStatus.blocked


# ── run_until_idle ──────────────────────────────────────────────────────────


def test_run_until_idle_completes_a_simple_obligation() -> None:
    """End-to-end: empty db plan-execute-verify cycles until idle."""
    db = _empty_db()
    with db.session() as s:
        s.add(
            Obligation(
                title="end to end",
                completion_criteria=[{"type": "principal_ratification", "ratified": True}],
            )
        )
        s.commit()
    loop = _loop(db, plan_developer=StubPlanDeveloper(steps=[{}, {}]))
    outcomes = loop.run_until_idle(max_ticks=10)
    closes = sum(o.obligations_closed for o in outcomes)
    assert closes == 1
    assert outcomes[-1].idle is True or not outcomes[-1].did_anything


def test_run_until_idle_respects_max_ticks_safety_cap() -> None:
    """Pathological case: an obligation that never closes shouldn't loop
    forever. Plan returns 0 steps every time → blocked, then re-developed,
    repeat. max_ticks must cap the loop."""
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="never closes"))
        s.commit()
    dev = StubPlanDeveloper(steps=[])  # 0-step plan → blocked immediately
    loop = _loop(db, plan_developer=dev)
    # Each tick develops a plan (because previous is blocked, not verified, but
    # blocked plans still count as active until manually marked verified).
    # The key is that did_anything stays True; max_ticks must stop it.
    outcomes = loop.run_until_idle(max_ticks=5)
    assert len(outcomes) <= 5


# ── Verifier directly ────────────────────────────────────────────────────────


def test_completion_verifier_unknown_criterion_fails_safely() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(
            title="t",
            completion_criteria=[{"type": "nope_does_not_exist"}],
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id
    v = CompletionVerifier(db)
    outcome = v.check(ob_id)
    assert outcome.all_passed is False
    assert "no verifier registered" in (outcome.results[0][1].note or "")


def test_completion_verifier_no_criteria_cannot_pass() -> None:
    """No criteria → all_passed=False (explicit criteria required per L20)."""
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(title="t", completion_criteria=[])
        s.add(ob)
        s.commit()
        ob_id = ob.id
    v = CompletionVerifier(db)
    outcome = v.check(ob_id)
    assert outcome.all_passed is False


def test_subtask_closed_verifier() -> None:
    db = _empty_db()
    with db.session() as s:
        sub = Obligation(title="subtask", status=ObligationStatus.done)
        s.add(sub)
        s.commit()
        sub_id = sub.id
        parent = Obligation(
            title="parent",
            completion_criteria=[{"type": "subtask_closed", "obligation_id": sub_id}],
        )
        s.add(parent)
        s.commit()
        parent_id = parent.id
    v = CompletionVerifier(db)
    assert v.check(parent_id).all_passed is True


def test_subtask_closed_verifier_fails_when_subtask_not_done() -> None:
    db = _empty_db()
    with db.session() as s:
        sub = Obligation(title="subtask", status=ObligationStatus.in_progress)
        s.add(sub)
        s.commit()
        sub_id = sub.id
        parent = Obligation(
            title="parent",
            completion_criteria=[{"type": "subtask_closed", "obligation_id": sub_id}],
        )
        s.add(parent)
        s.commit()
        parent_id = parent.id
    assert CompletionVerifier(db).check(parent_id).all_passed is False


def test_time_elapsed_verifier_passes_after_window() -> None:
    db = _empty_db()
    past = (utcnow() - timedelta(hours=10)).isoformat()
    with db.session() as s:
        ob = Obligation(
            title="t",
            completion_criteria=[
                {
                    "type": "time_elapsed_with_no_objection",
                    "since": past,
                    "hours": 1,
                }
            ],
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id
    assert CompletionVerifier(db).check(ob_id).all_passed is True


def test_time_elapsed_verifier_fails_with_objection() -> None:
    db = _empty_db()
    past = (utcnow() - timedelta(hours=10)).isoformat()
    with db.session() as s:
        ob = Obligation(
            title="t",
            completion_criteria=[
                {
                    "type": "time_elapsed_with_no_objection",
                    "since": past,
                    "hours": 1,
                    "objection_received": True,
                }
            ],
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id
    assert CompletionVerifier(db).check(ob_id).all_passed is False


def test_time_elapsed_verifier_fails_inside_window() -> None:
    db = _empty_db()
    recent = (utcnow() - timedelta(minutes=10)).isoformat()
    with db.session() as s:
        ob = Obligation(
            title="t",
            completion_criteria=[
                {
                    "type": "time_elapsed_with_no_objection",
                    "since": recent,
                    "hours": 1,
                }
            ],
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id
    assert CompletionVerifier(db).check(ob_id).all_passed is False


def test_completion_verifier_logs_check_rows() -> None:
    """Every criterion check writes a CompletionCheck row for audit."""
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(
            title="t",
            completion_criteria=[
                {"type": "principal_ratification", "ratified": True},
                {"type": "principal_ratification"},  # this one fails
            ],
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id
    CompletionVerifier(db).check(ob_id)
    from agent_core.state.models import CompletionCheck

    with db.session() as s:
        checks = list(s.exec(select(CompletionCheck)).all())
    assert len(checks) == 2
    passed_count = sum(1 for c in checks if c.passed)
    assert passed_count == 1


def test_register_custom_verifier() -> None:
    db = _empty_db()
    with db.session() as s:
        ob = Obligation(
            title="t",
            completion_criteria=[{"type": "magic", "value": 42}],
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id

    def magic_verifier(db, obligation_id, criterion):
        return CheckResult(passed=criterion.get("value") == 42)

    v = CompletionVerifier(db)
    v.register("magic", magic_verifier)
    assert v.check(ob_id).all_passed is True


def test_obligation_event_audit_after_full_cycle() -> None:
    """End-to-end: plan_developed → step_executed → completion_checked → closed
    audit events all land in obligation_event."""
    db = _empty_db()
    with db.session() as s:
        s.add(
            Obligation(
                title="audit me",
                status=ObligationStatus.inbox,  # so we also see status_changed
                completion_criteria=[{"type": "principal_ratification", "ratified": True}],
            )
        )
        s.commit()
    _loop(db, plan_developer=StubPlanDeveloper(steps=[{}])).run_until_idle()
    with db.session() as s:
        kinds = [e.kind for e in s.exec(select(ObligationEvent)).all()]
    for needed in (
        ObligationEventKind.plan_developed,
        ObligationEventKind.status_changed,
        ObligationEventKind.step_executed,
        ObligationEventKind.completion_checked,
        ObligationEventKind.closed,
    ):
        assert needed in kinds, f"missing audit event: {needed}"


def test_check_raises_for_unknown_obligation_id() -> None:
    db = _empty_db()
    v = CompletionVerifier(db)
    with pytest.raises(ValueError, match="not found"):
        v.check("never-existed")
