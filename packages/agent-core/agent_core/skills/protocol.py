"""Skill + LanguageModel Protocols.

Three things in this module:

  - ``Skill`` — the contract every registered skill satisfies. Has identity,
    schemas, an execute() that returns a SkillResult, and optional seed rules.

  - ``LanguageModel`` — the small Protocol skills depend on for LLM calls.
    Real Hermes satisfies it (vendored later); tests pass a StubLanguageModel.
    Keeping this thin means we don't pin skills to a vendor.

  - ``SeedRule`` — a learning rule a skill can pre-install when it's enabled.
    Same shape as the seed_packs/ YAML rules from sprint 5b.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

# ── LanguageModel Protocol ──────────────────────────────────────────────────


@runtime_checkable
class LanguageModel(Protocol):
    """Minimal LLM interface skills depend on. Hermes satisfies this.

    Why so thin: skills should be vendor-agnostic. If a skill needs more than
    chat-style completion (function calling, streaming, image input) it
    advertises a typed extension via a sibling Protocol — but the default
    contract stays at "give me text from text"."""

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str: ...


# ── SeedRule ────────────────────────────────────────────────────────────────


@dataclass
class SeedRule:
    """A learning rule a skill ships with.

    Installed into LearningStore when the skill is enabled so the skill has
    working defaults. Same data model as seed_packs/professional.yaml; the
    only difference is the rule travels with the skill code rather than a
    separate YAML file.
    """

    correction: str
    skill_tags: list[str] = field(default_factory=list)
    context: str = ""


# ── Skill Protocol ──────────────────────────────────────────────────────────


@runtime_checkable
class Skill(Protocol):
    """A named, invokable capability.

    Implementations may be classes or any object that satisfies this Protocol.
    The registry holds them and dispatches by ``name``.
    """

    name: str
    """Unique stable identifier (e.g. 'email-triage'). kebab-case convention."""

    description: str
    """One-line human description shown in the skill picker."""

    tags: list[str]
    """Discovery tags. ``email-triage`` might tag itself ``["email", "classify"]``."""

    input_schema: type[BaseModel]
    """Pydantic model the input dict is validated against."""

    output_schema: type[BaseModel]
    """Pydantic model the output dict is validated against."""

    seed_rules: list[SeedRule]
    """Learning rules installed when the skill is enabled. Empty list is fine."""

    def execute(
        self,
        input: BaseModel,  # noqa: A002 — matches the schema attr name
        context: SkillContext,  # forward ref; defined in context.py
    ) -> SkillResult: ...


__all__ = ["LanguageModel", "SeedRule", "Skill"]


# Late import to avoid circular dep — context.py imports from this module.
from agent_core.skills.context import SkillContext, SkillResult  # noqa: E402, F401
