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

    For tool-use tests (Sprint 24), set ``tool_call_responses`` to a list
    of CompletionResponse objects to cycle through. Each call to
    ``complete_with_tools`` returns the next entry; once exhausted, falls
    back to a content-only response built from the regular ``responses``
    / ``patterns`` / ``default`` fields.

    Records every call so tests can assert on what was sent.
    """

    responses: list[str] = field(default_factory=list)
    patterns: list[tuple[str, str]] = field(default_factory=list)
    default: str = "stub-response"
    calls: list[dict] = field(default_factory=list)
    tool_call_responses: list = field(default_factory=list)
    tool_calls_recorded: list[dict] = field(default_factory=list)
    _next_response_idx: int = 0
    _next_tool_response_idx: int = 0

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

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ):
        """Tool-use stub. Returns the next entry from
        ``tool_call_responses`` if any remain, otherwise builds a
        content-only CompletionResponse from the regular complete()
        machinery (so existing patterns/responses still work for
        chat scenarios that don't actually need tools)."""
        from agent_core.skills.tools import CompletionResponse

        self.tool_calls_recorded.append(
            {"system": system, "messages": messages, "tool_count": len(tools)}
        )

        if self._next_tool_response_idx < len(self.tool_call_responses):
            resp = self.tool_call_responses[self._next_tool_response_idx]
            self._next_tool_response_idx += 1
            return resp

        # Fallback: synthesize a content-only response using the same
        # logic as complete() but driven by the latest user message.
        latest_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                latest_user = m.get("content", "")
                break
        text = self.complete(
            system=system,
            user=latest_user,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return CompletionResponse(content=text, tool_calls=[])


__all__ = ["StubLanguageModel"]
