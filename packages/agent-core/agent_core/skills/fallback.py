"""FallbackLanguageModel: try primary, fall back on failure.

When the primary LLM goes down (Ollama process stopped, Tailscale link
flapping, hosted endpoint rate-limited or 5xx-ing), the autonomous tick
shouldn't grind to a halt. ``FallbackLanguageModel`` wraps two
``LanguageModel`` impls and tries the second only when the first raises
``LanguageModelError``.

Common pattern:
  - Primary  : local Ollama (free, private, fast when up)
  - Fallback : a hosted OpenAI-compat endpoint (DeepSeek, OpenAI,
               OpenRouter) — cheap and reliable enough that "the agent
               kept working through my laptop being asleep" is the
               daily experience.

The wrapper preserves the LanguageModel Protocol — both ``complete()``
and ``complete_with_tools()`` flow through with the same signatures.
Only ``LanguageModelError`` triggers fallback; other exceptions
(programmer errors, timeouts that the underlying impl already handles)
propagate so they don't get masked. The fallback's own errors propagate
unwrapped — there's no third tier.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_core.skills.openai_compat import LanguageModelError

logger = logging.getLogger(__name__)


class FallbackLanguageModel:
    """LanguageModel that tries ``primary`` first, then ``fallback``.

    Only catches ``LanguageModelError`` (network, status, malformed
    response, empty content). Other exception types propagate so bugs
    aren't swallowed.

    Both ``complete()`` and ``complete_with_tools()`` route through the
    same logic. If the primary lacks ``complete_with_tools`` but the
    fallback has it, the wrapper still uses the primary's plain
    ``complete()`` path — the wrapper doesn't auto-promote feature
    capabilities.
    """

    def __init__(self, primary: Any, fallback: Any) -> None:
        self.primary = primary
        self.fallback = fallback
        # Telemetry-friendly model_id surfaces both legs.
        primary_id = getattr(primary, "model_id", type(primary).__name__)
        fallback_id = getattr(fallback, "model_id", type(fallback).__name__)
        self.model_id = f"fallback({primary_id}→{fallback_id})"

    # ── complete ──────────────────────────────────────────────────────────

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        try:
            return self.primary.complete(
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except LanguageModelError as e:
            logger.warning(
                "primary LLM failed (%s); falling back to %s",
                e,
                getattr(self.fallback, "model_id", type(self.fallback).__name__),
            )
            return self.fallback.complete(
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature,
            )

    # ── complete_with_tools ───────────────────────────────────────────────

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ):
        # If the primary doesn't support tools but the fallback does, we
        # still send through the primary first (callers explicitly chose
        # which model is primary) — they can disable tools at the session
        # level if they want fallback-only behavior.
        if not hasattr(self.primary, "complete_with_tools"):
            # Primary won't be tried for tools — fall straight to the
            # plain completion path so we don't silently skip the user's
            # primary choice.
            return self.primary.complete(
                system=system,
                user=_flatten_for_plain_complete(messages),
                max_tokens=max_tokens,
                temperature=temperature,
            )

        try:
            return self.primary.complete_with_tools(
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except LanguageModelError as e:
            logger.warning(
                "primary LLM (tool-use) failed (%s); falling back to %s",
                e,
                getattr(self.fallback, "model_id", type(self.fallback).__name__),
            )
            if hasattr(self.fallback, "complete_with_tools"):
                return self.fallback.complete_with_tools(
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            # Fallback doesn't support tools — degrade to plain completion.
            return self.fallback.complete(
                system=system,
                user=_flatten_for_plain_complete(messages),
                max_tokens=max_tokens,
                temperature=temperature,
            )


def _flatten_for_plain_complete(messages: list[dict]) -> str:
    """Compress a tools-format messages list into a single user prompt.

    Used when degrading from a tool-use LM to a plain-complete one. Drops
    role markers and tool-call structure; keeps only content text in
    arrival order.
    """
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if content:
            parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


__all__ = ["FallbackLanguageModel"]
