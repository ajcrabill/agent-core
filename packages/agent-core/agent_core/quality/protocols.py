"""Abstract interfaces for the model side of the quality auditor.

The auditor calls a model to produce a score. Like the agent loop, the
calling layer is dependency-injected via Protocol so the orchestrator is
testable without Hermes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class AuditScore:
    """The model's evaluation of one piece of work.

    `score` is 0.0–1.0 (higher is better). `passed` is the auditor's own
    boolean (it might pass at a different threshold than the orchestrator's
    default — the orchestrator stores both and uses its own threshold for
    undelegation).
    """

    score: float
    passed: bool
    primary_notes: str | None = None


@runtime_checkable
class AuditorModel(Protocol):
    """Produce an audit score for a piece of subject work.

    Real implementations (Hermes-backed, sprint TBD) call a model with a
    rubric prompt. Tests use a stub returning fixed scores.
    """

    def audit(
        self,
        *,
        task_type: str,
        subject_model: str,
        output_summary: str,
        rubrics: list[str] | None = None,
    ) -> AuditScore: ...


__all__ = ["AuditScore", "AuditorModel"]
