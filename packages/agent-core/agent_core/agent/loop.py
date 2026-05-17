"""Goal-directed agent loop.

The orchestration that makes "bias for action" run. Per L20:

  While there are active agent-owned obligations, develop a plan or execute
  the next plan step. Sleep only when no actionable next step exists.

One tick of the loop:

  1. Load top-N active agent-owned obligations (via ContextLoader's same
     query — guarantees what the model sees matches what the loop works on)
  2. For each obligation:
       - If no plan exists → develop one (PlanDeveloper)
       - Elif plan has next step → execute it (StepExecutor); log ActionLog
       - Else (plan steps exhausted) → run completion verifier; if all pass,
         close the obligation; else mark plan blocked
     Each branch logs an ObligationEvent for audit visibility.
  3. Return a LoopOutcome summarizing what happened.

The loop is structurally independent of the model-calling layer: PlanDeveloper
and StepExecutor are Protocols. Real Hermes-backed implementations land in a
later sprint; tests use simple mocks.

Per L20, every action's `obligation_id` is set by the loop (not the executor),
guaranteeing the "every action traces to an obligation" invariant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel import select

from agent_core.agent.context import ContextLoader, ContextScope
from agent_core.agent.protocols import PlanDeveloper, StepExecutor
from agent_core.agent.verify import CompletionVerifier, VerifyOutcome
from agent_core.state.db import Database
from agent_core.state.models import (
    ActionLog,
    Obligation,
    ObligationEvent,
    ObligationEventKind,
    ObligationOwner,
    ObligationStatus,
    Plan,
    PlanStatus,
    utcnow,
)

logger = logging.getLogger(__name__)


# ── Outcome types ────────────────────────────────────────────────────────────


@dataclass
class TickOutcome:
    """What one tick produced. The runner can use this to decide whether to
    keep ticking or sleep."""

    plans_developed: int = 0
    steps_executed: int = 0
    completion_checks_run: int = 0
    obligations_closed: int = 0
    obligations_blocked: int = 0
    obligations_seen: int = 0
    idle: bool = False  # true if there were no agent-owned active obligations

    # For tests + telemetry
    closed_ids: list[str] = field(default_factory=list)
    blocked_ids: list[str] = field(default_factory=list)
    failed_verify_outcomes: list[VerifyOutcome] = field(default_factory=list)

    @property
    def did_anything(self) -> bool:
        return any(
            (
                self.plans_developed,
                self.steps_executed,
                self.completion_checks_run,
                self.obligations_closed,
                self.obligations_blocked,
            )
        )


# ── Loop ─────────────────────────────────────────────────────────────────────


class AgentLoop:
    """Drive plan-or-execute-or-verify across all active agent-owned
    obligations.

    Construction is dependency-injection: pass implementations of
    PlanDeveloper and StepExecutor (plus a CompletionVerifier for the
    completion branch). Tests use simple mocks; production uses Hermes-
    backed implementations.
    """

    def __init__(
        self,
        db: Database,
        context_loader: ContextLoader,
        plan_developer: PlanDeveloper,
        step_executor: StepExecutor,
        completion_verifier: CompletionVerifier,
        *,
        max_obligations_per_tick: int = 5,
    ) -> None:
        self.db = db
        self.context_loader = context_loader
        self.plan_developer = plan_developer
        self.step_executor = step_executor
        self.completion_verifier = completion_verifier
        self.max_obligations_per_tick = max_obligations_per_tick

    @classmethod
    def from_settings(
        cls,
        settings: object,
        db: Database,
        context_loader: ContextLoader,
        plan_developer: PlanDeveloper,
        step_executor: StepExecutor,
        completion_verifier: CompletionVerifier,
    ) -> AgentLoop:
        """Build from ``AgentSettings``: reads ``settings.runtime.max_obligations_per_tick``.

        Note: ``max_ticks_safety_cap`` from settings is not stored on the
        instance — it's passed at ``run()`` time. Callers using this factory
        should also pass ``settings.runtime.max_ticks_safety_cap`` to ``run()``.
        """
        return cls(
            db,
            context_loader,
            plan_developer,
            step_executor,
            completion_verifier,
            max_obligations_per_tick=settings.runtime.max_obligations_per_tick,  # type: ignore[attr-defined]
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def tick(self, scope: ContextScope | None = None) -> TickOutcome:
        """One iteration of the loop. Returns a summary of what happened."""
        outcome = TickOutcome()

        active_ids = self._load_active_obligation_ids()
        outcome.obligations_seen = len(active_ids)
        if not active_ids:
            outcome.idle = True
            return outcome

        for ob_id in active_ids:
            self._step_one(ob_id, scope, outcome)

        return outcome

    def run_until_idle(
        self,
        *,
        max_ticks: int = 100,
        scope: ContextScope | None = None,
    ) -> list[TickOutcome]:
        """Tick repeatedly until no actionable obligation remains, or until
        ``max_ticks`` is reached (safety cap; runaway loops shouldn't be
        possible by design, but a cap is cheap insurance).

        Stops when a tick is idle OR did nothing meaningful (e.g., every
        obligation is waiting for external input).
        """
        outcomes: list[TickOutcome] = []
        for _ in range(max_ticks):
            outcome = self.tick(scope)
            outcomes.append(outcome)
            if outcome.idle or not outcome.did_anything:
                break
        return outcomes

    # ── One-obligation step ────────────────────────────────────────────────

    def _step_one(
        self,
        obligation_id: str,
        scope: ContextScope | None,
        outcome: TickOutcome,
    ) -> None:
        active_plan = self._get_active_plan(obligation_id)
        # Refresh context per-obligation so the plan/execute branch sees the
        # latest state (rules, peer messages, incidents) — cheap.
        ctx_scope = scope or ContextScope(obligation_id=obligation_id)
        if ctx_scope.obligation_id is None:
            ctx_scope = ContextScope(
                skill=ctx_scope.skill,
                obligation_id=obligation_id,
                inbound_kind=ctx_scope.inbound_kind,
            )
        context = self.context_loader.collect(ctx_scope)

        if active_plan is None:
            self._develop_plan_branch(obligation_id, context, outcome)
            return

        if active_plan.current_step < len(active_plan.steps):
            self._execute_step_branch(active_plan, context, outcome)
            return

        # Plan steps exhausted → verify completion
        self._verify_completion_branch(obligation_id, active_plan, outcome)

    # ── Branches ────────────────────────────────────────────────────────────

    def _develop_plan_branch(self, obligation_id: str, context, outcome: TickOutcome) -> None:
        with self.db.session() as s:
            ob = s.get(Obligation, obligation_id)
            if ob is None or ob.status == ObligationStatus.done:
                return
            proposal = self.plan_developer.develop(ob, context)
            plan = Plan(
                obligation_id=obligation_id,
                steps=proposal.steps,
                confidence=proposal.confidence,
                status=(PlanStatus.executing if proposal.steps else PlanStatus.blocked),
            )
            s.add(plan)
            s.add(
                ObligationEvent(
                    obligation_id=obligation_id,
                    kind=ObligationEventKind.plan_developed,
                    actor="agent",
                    payload={
                        "plan_id": plan.id,
                        "step_count": len(proposal.steps),
                        "confidence": proposal.confidence,
                        "rationale": proposal.rationale,
                    },
                )
            )
            # Mark obligation as in_progress on first plan if it was inbox
            if ob.status == ObligationStatus.inbox:
                ob.status = ObligationStatus.in_progress
                ob.started_at = utcnow()
                s.add(
                    ObligationEvent(
                        obligation_id=obligation_id,
                        kind=ObligationEventKind.status_changed,
                        actor="agent",
                        payload={"to": "in-progress", "reason": "plan_developed"},
                    )
                )
            s.commit()
            outcome.plans_developed += 1

    def _execute_step_branch(self, plan: Plan, context, outcome: TickOutcome) -> None:
        step_index = plan.current_step
        result = self.step_executor.execute(plan, step_index, context)
        with self.db.session() as s:
            # Persist the action — REQUIRES obligation_id per L20
            s.add(
                ActionLog(
                    obligation_id=plan.obligation_id,
                    action_class=result.action_class,
                    target=result.target,
                    rationale=result.rationale,
                    outcome=result.outcome,
                    error=result.error,
                )
            )
            # Advance the plan's pointer
            plan_row = s.get(Plan, plan.id)
            if plan_row is None:
                s.commit()
                return
            plan_row.current_step = step_index + 1
            plan_row.updated_at = utcnow()
            s.add(plan_row)
            s.add(
                ObligationEvent(
                    obligation_id=plan.obligation_id,
                    kind=ObligationEventKind.step_executed,
                    actor="agent",
                    payload={
                        "plan_id": plan.id,
                        "step_index": step_index,
                        "action_class": result.action_class.value,
                        "outcome": result.outcome.value,
                    },
                )
            )
            s.commit()
            outcome.steps_executed += 1

    def _verify_completion_branch(
        self,
        obligation_id: str,
        plan: Plan,
        outcome: TickOutcome,
    ) -> None:
        verify_outcome = self.completion_verifier.check(obligation_id)
        outcome.completion_checks_run += 1

        with self.db.session() as s:
            ob = s.get(Obligation, obligation_id)
            plan_row = s.get(Plan, plan.id)
            if ob is None:
                s.commit()
                return

            s.add(
                ObligationEvent(
                    obligation_id=obligation_id,
                    kind=ObligationEventKind.completion_checked,
                    actor="agent",
                    payload={
                        "all_passed": verify_outcome.all_passed,
                        "checked_count": len(verify_outcome.results),
                        "failed_count": len(verify_outcome.failures),
                    },
                )
            )

            if verify_outcome.all_passed:
                ob.status = ObligationStatus.done
                ob.completed_at = utcnow()
                if plan_row is not None:
                    plan_row.status = PlanStatus.verified
                    plan_row.updated_at = utcnow()
                    s.add(plan_row)
                s.add(ob)
                s.add(
                    ObligationEvent(
                        obligation_id=obligation_id,
                        kind=ObligationEventKind.closed,
                        actor="agent",
                        payload={"reason": "completion_criteria_met"},
                    )
                )
                outcome.obligations_closed += 1
                outcome.closed_ids.append(obligation_id)
            else:
                # Mark plan blocked so we don't loop forever on the same plan
                # waiting for an external signal. The next loop iteration will
                # see plan.status='blocked' and develop a NEW plan if needed.
                if plan_row is not None:
                    plan_row.status = PlanStatus.blocked
                    plan_row.updated_at = utcnow()
                    s.add(plan_row)
                outcome.obligations_blocked += 1
                outcome.blocked_ids.append(obligation_id)
                outcome.failed_verify_outcomes.append(verify_outcome)
            s.commit()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _load_active_obligation_ids(self) -> list[str]:
        """Same shape of query as ContextLoader's obligations block, for
        consistency. Returns up to ``max_obligations_per_tick`` IDs."""
        with self.db.session() as s:
            stmt = (
                select(Obligation)
                .where(Obligation.owner == ObligationOwner.agent)
                .where(Obligation.status != ObligationStatus.done)
            )
            rows = list(s.exec(stmt).all())
            # Sort by same key the context loader uses for stable behavior
            from agent_core.agent.context import _obligation_sort_key

            rows.sort(key=_obligation_sort_key)
            return [r.id for r in rows[: self.max_obligations_per_tick]]

    def _get_active_plan(self, obligation_id: str) -> Plan | None:
        """Most recent non-verified plan for this obligation, or None."""
        with self.db.session() as s:
            stmt = (
                select(Plan)
                .where(Plan.obligation_id == obligation_id)
                .where(Plan.status != PlanStatus.verified)
                .order_by(Plan.created_at.desc())
            )
            return s.exec(stmt).first()


__all__ = [
    "AgentLoop",
    "TickOutcome",
]
