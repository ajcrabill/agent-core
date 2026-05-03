"""SkillContext — what a skill gets when it runs.

The context bundles every cross-cutting capability a skill might need:

  - the agent's settings (so the skill knows the autonomy posture)
  - the database (for learning rules, recent thoughts, etc.)
  - the OpenBrain store (for semantic recall)
  - the language model (for LLM calls)
  - the learning store (so the skill can read applicable rules)

Skills don't construct any of these — the runner builds the context once
and passes it in. This makes skills trivially testable: hand the test bed's
StubLanguageModel + an in-memory db and the skill runs end-to-end.

Skills that DON'T need a piece (e.g. a pure-rules skill that never calls an
LLM) can leave it None on construction; the type system flags the access if
the skill mistakenly tries to use it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


@dataclass
class SkillResult:
    """What a skill execute() returns.

    ``output`` is the validated output schema instance (Pydantic model). The
    SkillRunner re-validates against the skill's declared output_schema so a
    skill that returns the wrong shape fails loudly at the boundary.
    """

    output: BaseModel
    confidence: float
    """0.0–1.0 — feeds calibration. 1.0 = "I'm very sure"; 0.0 = "guess."""
    rationale: str = ""
    """Short why-string for the action_log + audit trail."""
    references: list[dict[str, Any]] = field(default_factory=list)
    """Citations from openbrain or other sources. Each ref typically has
    {source_kind, source_uri, snippet}."""


@dataclass
class SkillContext:
    """Everything a running skill might reach for. Construct once per call."""

    settings: object  # AgentSettings
    db: Any  # Database
    language_model: object | None = None  # LanguageModel
    openbrain: object | None = None  # OpenBrainStore
    learning_store: object | None = None  # LearningStore
    skill_session: dict[str, Any] = field(default_factory=dict)
    """Per-call scratchpad. Skills can read/write; the runner persists it
    onto the resulting QualityAudit row so the user can see why the skill
    decided what it did."""

    # Convenience accessors — keep skill bodies clean of getattr noise.

    @property
    def autonomy(self):
        return self.settings.autonomy  # type: ignore[attr-defined]

    @property
    def learning(self):
        return self.settings.learning  # type: ignore[attr-defined]


__all__ = ["SkillContext", "SkillResult"]
