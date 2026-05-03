"""Abstract interfaces between the agent loop and the model-calling layer.

The agent loop (loop.py) is structurally independent of how plans are produced
or steps are executed — those are model-calling concerns that need Hermes
(deferred to a later sprint). The loop accepts injectable implementations that
satisfy these protocols.

In production: real implementations call Hermes with a context bundle.
In tests: simple mock implementations exercise the loop's branching.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_core.agent.context import ContextBundle
from agent_core.state.models import ActionClass, ActionOutcome, Obligation, Plan

# ── Result types for protocol implementations ────────────────────────────────


class PlanProposal:
    """What a PlanDeveloper produces. Not a SQLModel row — the loop is
    responsible for persisting (so it can attach obligation_id, ts, etc.)."""

    def __init__(
        self,
        steps: list[dict],
        confidence: float = 0.0,
        rationale: str | None = None,
    ) -> None:
        self.steps = steps
        self.confidence = confidence
        self.rationale = rationale


class StepResult:
    """What a StepExecutor produces. The loop persists this as an ActionLog row.

    `obligation_id` is set by the loop, not the executor. Per L20, every
    action traces to an obligation.
    """

    def __init__(
        self,
        action_class: ActionClass,
        outcome: ActionOutcome,
        target: str | None = None,
        rationale: str | None = None,
        error: str | None = None,
    ) -> None:
        self.action_class = action_class
        self.outcome = outcome
        self.target = target
        self.rationale = rationale
        self.error = error


# ── Protocols ────────────────────────────────────────────────────────────────


@runtime_checkable
class PlanDeveloper(Protocol):
    """Build a plan for satisfying an obligation.

    Inputs: the obligation (with its completion_criteria) and a ContextBundle
    (so the developer sees current rules, peer messages, open incidents).

    Output: a PlanProposal (loop persists it as a Plan row).
    """

    def develop(
        self,
        obligation: Obligation,
        context: ContextBundle,
    ) -> PlanProposal: ...


@runtime_checkable
class StepExecutor(Protocol):
    """Execute one step of a plan.

    Inputs: the plan, the index of the step to execute, current context.

    Output: a StepResult that the loop will persist as an ActionLog row
    (with obligation_id auto-attached).
    """

    def execute(
        self,
        plan: Plan,
        step_index: int,
        context: ContextBundle,
    ) -> StepResult: ...


__all__ = [
    "PlanDeveloper",
    "PlanProposal",
    "StepExecutor",
    "StepResult",
]
