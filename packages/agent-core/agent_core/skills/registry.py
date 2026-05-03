"""SkillRegistry — register skills by name, look them up by name or tag.

dcos-agent and ikb-agent each ship their own default skills. Skill packages
(future) extend either product. The registry is the discovery point.

A process-wide ``default_registry`` is provided for convenience — products
populate it at import time:

    # in dcos_agent/skills/__init__.py
    from agent_core.skills import default_registry
    from dcos_agent.skills.email_triage import EmailTriage
    default_registry.register(EmailTriage())

Tests typically build their own registry to avoid leaking skill registrations
across test files.
"""

from __future__ import annotations

import logging

from agent_core.skills.protocol import Skill

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Holds Skill instances. Look up by name; filter by tag."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Add a skill. Raises if ``skill.name`` is already registered (so a
        package conflict surfaces at import time, not later in production)."""
        if not skill.name:
            raise ValueError("skill.name must be a non-empty string")
        if skill.name in self._skills:
            raise ValueError(
                f"skill {skill.name!r} already registered; pick a unique name "
                f"or unregister first"
            )
        self._skills[skill.name] = skill
        logger.info("registered skill: %s (%s)", skill.name, ",".join(skill.tags))

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def require(self, name: str) -> Skill:
        """Like ``get`` but raises with a helpful message — for callers that
        know the skill should exist."""
        skill = self._skills.get(name)
        if skill is None:
            available = sorted(self._skills.keys())
            raise KeyError(
                f"skill {name!r} not found; registered: {available}"
            )
        return skill

    def list(self) -> list[Skill]:
        """All registered skills, sorted by name for stable display."""
        return [self._skills[k] for k in sorted(self._skills)]

    def by_tag(self, tag: str) -> list[Skill]:
        """All skills carrying ``tag`` (sorted by name)."""
        return [s for s in self.list() if tag in s.tags]

    def names(self) -> list[str]:
        return sorted(self._skills.keys())

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills


# Process-wide default. Products populate this at import time.
default_registry = SkillRegistry()


__all__ = ["SkillRegistry", "default_registry"]
