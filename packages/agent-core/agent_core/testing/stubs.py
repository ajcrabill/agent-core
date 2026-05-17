"""Reusable stub implementations of every injectable Protocol.

Lifted from the per-module test files so dcos-agent / ikb-agent / skill
packages can use them too. Each stub is small, deterministic, and
configurable enough to drive the relevant component through happy and
sad paths without requiring Hermes or other live infrastructure.

These are TESTING utilities — not production code. They are intentionally
crude and may have edge-case behavior unsuitable for real workloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_core.agent.context import ContextBundle
from agent_core.agent.protocols import PlanProposal, StepResult
from agent_core.content_creation.diff_extractor import ProposedRule
from agent_core.quality.protocols import AuditScore
from agent_core.state.models import (
    ActionClass,
    ActionOutcome,
    Obligation,
    Plan,
)

# ── Plan + Step ─────────────────────────────────────────────────────────────


@dataclass
class StubPlanDeveloper:
    """Returns a fixed plan every time. Counts calls.

    Args:
        steps: list of dicts (defaults to a 2-step read+write plan).
        confidence: returned PlanProposal confidence (default 0.9).
        rationale: returned PlanProposal rationale.
    """

    steps: list[dict] | None = None
    confidence: float = 0.9
    rationale: str = "stub-developer"
    calls: int = 0

    def __post_init__(self) -> None:
        if self.steps is None:
            self.steps = [
                {"description": "step 0", "action_class": "read"},
                {"description": "step 1", "action_class": "write_internal"},
            ]

    def develop(self, obligation: Obligation, context: ContextBundle) -> PlanProposal:
        self.calls += 1
        return PlanProposal(
            steps=list(self.steps or []),
            confidence=self.confidence,
            rationale=self.rationale,
        )


@dataclass
class StubStepExecutor:
    """Returns the same StepResult every time. Records calls."""

    action_class: ActionClass = ActionClass.read
    outcome: ActionOutcome = ActionOutcome.succeeded
    rationale: str = "stub-executor"
    calls: list[tuple[str, int]] = field(default_factory=list)
    """List of (plan_id, step_index) tuples — for assertions."""

    def execute(self, plan: Plan, step_index: int, context: ContextBundle) -> StepResult:
        self.calls.append((plan.id, step_index))
        return StepResult(
            action_class=self.action_class,
            outcome=self.outcome,
            target=f"step:{step_index}",
            rationale=self.rationale,
        )


# ── Auditor ─────────────────────────────────────────────────────────────────


@dataclass
class StubAuditorModel:
    """Returns a fixed audit score. Records every call.

    Args:
        score: 0.0–1.0 (default 0.9 — passes most thresholds).
        passed: model's own pass/fail signal (default True).
        notes: optional primary_notes string.
    """

    score: float = 0.9
    passed: bool = True
    notes: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def audit(
        self,
        *,
        task_type: str,
        subject_model: str,
        output_summary: str,
        rubrics: list[str] | None = None,
    ) -> AuditScore:
        self.calls.append(
            {
                "task_type": task_type,
                "subject_model": subject_model,
                "output_summary": output_summary,
                "rubrics": rubrics or [],
            }
        )
        return AuditScore(score=self.score, passed=self.passed, primary_notes=self.notes)


# ── Diff extractor ──────────────────────────────────────────────────────────


@dataclass
class StubDiffExtractor:
    """Returns a fixed ProposedRule (or None) every time.

    Use ``returns=None`` to simulate "this correction doesn't generalize."
    """

    rule_text: str = "Always include the Q-prefix in deal IDs."
    skill_tags: list[str] = field(default_factory=lambda: ["general"])
    confidence: float = 0.85
    rationale: str = "stub-extractor"
    returns: ProposedRule | None | str = "default"
    """Either a literal ProposedRule, None, or 'default' (build from fields)."""
    calls: list[dict[str, Any]] = field(default_factory=list)

    def extract(
        self,
        *,
        original: str,
        corrected: str,
        narrative: str | None = None,
        skill: str | None = None,
    ) -> ProposedRule | None:
        self.calls.append({"original": original, "corrected": corrected, "skill": skill})
        if self.returns is None:
            return None
        if isinstance(self.returns, ProposedRule):
            return self.returns
        return ProposedRule(
            correction=self.rule_text,
            skill_tags=list(self.skill_tags),
            confidence=self.confidence,
            rationale=self.rationale,
        )


# ── Completion verifier (loop-aware) ───────────────────────────────────────


class StubCompletionVerifier:
    """Always reports an obligation complete on the first check.

    The real CompletionVerifier walks the obligation's completion_criteria
    and returns a structured VerifyOutcome. For tests that don't care about
    completion logic (most loop tests), this stub short-circuits to "done".

    For tests that DO care, use the real CompletionVerifier with the default
    verifier registry — that's what the production wiring uses.
    """

    def __init__(self, *, complete: bool = True) -> None:
        self.complete = complete
        self.calls: list[str] = []

    def verify(self, obligation_id: str):
        from agent_core.agent.verify import VerifyOutcome

        self.calls.append(obligation_id)
        return VerifyOutcome(
            obligation_id=obligation_id,
            all_passed=self.complete,
            results=[],
        )


__all__ = [
    "StubAuditorModel",
    "StubCompletionVerifier",
    "StubDiffExtractor",
    "StubPlanDeveloper",
    "StubStepExecutor",
]
