"""DiffExtractor — Protocol for the LLM step that turns a correction into a rule.

When the user corrects the agent's draft (Iteration.add_correction), an
LLM-driven diff-extractor inspects the change and proposes a learning rule
that would prevent the same correction in the future.

The chat layer wires this in: after a correction lands, run the extractor;
if it returns a ProposedRule with high confidence, surface it as a
CorrectionCandidate for one-click promotion.

Real implementations come with the Hermes vendor sprint; tests use a stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ProposedRule:
    """A rule the diff-extractor thinks would prevent this correction.

    Lands as a CorrectionCandidate (Sprint 5a) so the user can one-click
    promote it to a real LearningRule.
    """

    correction: str
    skill_tags: list[str]
    confidence: float
    rationale: str | None = None


@runtime_checkable
class DiffExtractor(Protocol):
    """Inspect a (original, corrected, narrative) triple and propose a rule
    that would have produced the corrected version on the first attempt.

    Returns None when the correction is too one-off to generalize (e.g.,
    typo fixes, factual corrections specific to this single document).
    """

    def extract(
        self,
        *,
        original: str,
        corrected: str,
        narrative: str | None = None,
        skill: str | None = None,
    ) -> ProposedRule | None: ...


__all__ = ["DiffExtractor", "ProposedRule"]
