"""SkillRunner — invoke a registered skill with input + return validated result.

Handles the housekeeping a skill shouldn't have to think about:

  - Validates input against the skill's input_schema (catches bad shape at
    the boundary, not deep inside the skill).
  - Re-validates the SkillResult.output against output_schema (catches a
    skill returning the wrong shape).
  - Catches exceptions and surfaces them as a structured failure (so a
    crashing skill doesn't take down the caller).

Doesn't yet hook into the QualityAuditor / calibration loops — that wiring
lands when the agent loop dispatches via SkillRunner. For now the runner
is the standalone front door for ``dcos skills run <name>`` and tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from agent_core.skills.context import SkillContext, SkillResult
from agent_core.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass
class RunOutcome:
    """Result of ``SkillRunner.run`` — succeeded or didn't, with details."""

    skill: str
    succeeded: bool
    result: SkillResult | None = None
    error: str | None = None


# ── Errors ──────────────────────────────────────────────────────────────────


class SkillInputError(ValueError):
    """Input failed the skill's input_schema validation."""


class SkillOutputError(ValueError):
    """Skill returned output that failed its output_schema validation."""


class SkillNotFoundError(KeyError):
    """No skill registered under that name."""


# ── Runner ──────────────────────────────────────────────────────────────────


class SkillRunner:
    """Invoke skills by name with a shared context.

    Stateless — construct one per request or share a single instance; both
    are fine."""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def run(
        self,
        name: str,
        input: dict[str, Any],  # noqa: A002 — match Skill.execute signature
        context: SkillContext,
    ) -> RunOutcome:
        """Validate, execute, validate output. Returns RunOutcome — never
        raises (errors land in ``RunOutcome.error``). Caller decides what
        to do with a failure."""
        skill = self.registry.get(name)
        if skill is None:
            return RunOutcome(
                skill=name,
                succeeded=False,
                error=f"skill {name!r} not registered",
            )

        # Input validation
        try:
            validated_input = skill.input_schema.model_validate(input)
        except ValidationError as e:
            return RunOutcome(
                skill=name,
                succeeded=False,
                error=f"input failed validation: {e}",
            )

        # Execute
        try:
            result = skill.execute(validated_input, context)
        except Exception as e:  # defensive — a skill bug shouldn't crash callers
            logger.exception("skill %s raised", name)
            return RunOutcome(skill=name, succeeded=False, error=f"skill raised: {e}")

        # Output validation
        try:
            skill.output_schema.model_validate(result.output)
        except ValidationError as e:
            return RunOutcome(
                skill=name,
                succeeded=False,
                error=f"output failed validation: {e}",
            )

        return RunOutcome(skill=name, succeeded=True, result=result)


__all__ = [
    "RunOutcome",
    "SkillInputError",
    "SkillNotFoundError",
    "SkillOutputError",
    "SkillRunner",
]
