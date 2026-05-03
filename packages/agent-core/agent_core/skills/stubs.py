"""Test stubs for the skill framework.

``StubLanguageModel`` is the default LLM stand-in for tests + offline
development. Returns canned text by matching the system prompt against a
list of pattern→response pairs (or just the first registered response).

Lives in agent_core.skills (rather than agent_core.testing) because skill
authors will reach for it when writing skill-level unit tests, and we want
the import path to be obvious.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class StubLanguageModel:
    """Deterministic LanguageModel for tests.

    Configure either:
      - ``responses=[str, str, ...]`` to cycle through, OR
      - ``patterns=[(regex, response), ...]`` to match by system prompt.

    Records every call so tests can assert on what the skill sent.
    """

    responses: list[str] = field(default_factory=list)
    patterns: list[tuple[str, str]] = field(default_factory=list)
    default: str = "stub-response"
    calls: list[dict] = field(default_factory=list)
    _next_response_idx: int = 0

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        # Pattern match against system prompt first
        for pattern, response in self.patterns:
            if re.search(pattern, system):
                return response
        # Cycle through canned responses
        if self.responses:
            response = self.responses[self._next_response_idx % len(self.responses)]
            self._next_response_idx += 1
            return response
        return self.default


__all__ = ["StubLanguageModel"]
