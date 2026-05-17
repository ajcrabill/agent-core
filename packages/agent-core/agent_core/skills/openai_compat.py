"""OpenAI-compatible LanguageModel implementation.

The default ``LanguageModel`` for production. Speaks the OpenAI
chat-completions wire format, which is the de-facto standard most
providers expose:

  - **OpenAI**         https://api.openai.com/v1
  - **OpenRouter**     https://openrouter.ai/api/v1   (universal gateway:
                                                       Anthropic, Google,
                                                       Meta, …)
  - **DeepSeek**       https://api.deepseek.com/v1
  - **Mistral**        https://api.mistral.ai/v1
  - **Together**       https://api.together.xyz/v1
  - **Groq**           https://api.groq.com/openai/v1
  - **Fireworks**      https://api.fireworks.ai/inference/v1
  - **Local Ollama**   http://localhost:11434/v1
  - **llama.cpp**      http://localhost:8080/v1

If a provider's wire format is OpenAI-compatible, this class talks to it.
Anthropic-native users can route through OpenRouter or LiteLLM.

Auth: ``Authorization: Bearer <api_key>`` header. ``api_key=None`` is
allowed (Ollama doesn't require a key) — the header is omitted.

Errors:
  - 4xx, 5xx → ``LanguageModelError`` with status + response body.
  - timeout → ``LanguageModelError`` (transient; consumer can retry).
  - empty content / malformed JSON → ``LanguageModelError``.

The class deliberately stays thin — no retry logic, no streaming, no
function-calling. Skills layer in retries; future Protocol extensions
add streaming / tool-calling when needed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


class LanguageModelError(RuntimeError):
    """Raised when the LLM call fails (network, status, malformed response)."""


@dataclass
class OpenAICompatLanguageModel:
    """Talks to any OpenAI-compatible chat-completions endpoint.

    Args:
        base_url: e.g. ``https://api.openai.com/v1`` (no trailing slash needed).
        model: the model name passed in the request body.
        api_key: bearer token, or None for keyless endpoints (Ollama).
        timeout: HTTP timeout in seconds. Default 60.
        default_max_tokens: model output ceiling when caller doesn't specify.
        default_temperature: sampling temperature when caller doesn't specify.
    """

    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = 60.0
    default_max_tokens: int = 2048
    default_temperature: float = 0.0

    @property
    def model_id(self) -> str:
        """Stable identifier for telemetry / audit logs."""
        return f"openai_compat:{self.base_url}#{self.model}"

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        _retry_count: int = 0,
    ) -> str:
        """Send a chat-completions request, return the model's text reply.

        Raises ``LanguageModelError`` on any failure — network, status,
        empty content, malformed JSON. Skills decide whether to retry.

        Reasoning-model handling: Ollama-hosted models like qwen3 and
        gemma3/4 emit a custom ``reasoning`` field with their thinking
        and leave ``content`` empty until they reach a final answer.
        With too-small max_tokens, the answer never lands. We auto-
        retry once with double the budget when finish_reason='length'
        and content is empty. After that, if content is still empty
        but reasoning is present, we return the reasoning as a
        best-effort fallback (better than crashing the caller).
        """
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        effective_max_tokens = max_tokens or self.default_max_tokens
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": effective_max_tokens,
            "temperature": (temperature if temperature is not None else self.default_temperature),
        }

        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise LanguageModelError(
                f"timeout after {self.timeout}s talking to {self.base_url}"
            ) from e
        except httpx.HTTPError as e:
            raise LanguageModelError(f"network error to {self.base_url}: {e}") from e

        if resp.status_code >= 400:
            # Truncate body in error message — providers occasionally return
            # huge HTML error pages.
            body = resp.text[:500]
            raise LanguageModelError(f"{self.base_url} returned {resp.status_code}: {body}")

        try:
            data = resp.json()
        except ValueError as e:
            raise LanguageModelError(f"{self.base_url} returned non-JSON: {resp.text[:200]}") from e

        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LanguageModelError(
                f"unexpected response shape from {self.base_url}: {data}"
            ) from e

        content = message.get("content")
        finish_reason = choice.get("finish_reason")
        reasoning = message.get("reasoning")

        # Reasoning-model retry: empty content + truncated by length →
        # the model was still thinking when we cut it off. Retry once
        # with 2x the budget. Cap retries so a degenerate model can't
        # exhaust the timeout.
        if (
            (not content or not isinstance(content, str))
            and finish_reason == "length"
            and _retry_count < 1
        ):
            logger.info(
                "%s returned empty content with finish=length; retrying with %d max_tokens",
                self.base_url,
                effective_max_tokens * 2,
            )
            return self.complete(
                system=system,
                user=user,
                max_tokens=effective_max_tokens * 2,
                temperature=temperature,
                _retry_count=_retry_count + 1,
            )

        if isinstance(content, str) and content:
            return content

        # Best-effort fallback: if a reasoning model returned only its
        # thinking trace, surface that. The skill's parser may extract a
        # JSON payload from it; otherwise the caller will fail more
        # gracefully than a "no content" panic.
        if isinstance(reasoning, str) and reasoning:
            logger.warning(
                "%s returned empty content; falling back to reasoning field (%d chars)",
                self.base_url,
                len(reasoning),
            )
            return reasoning

        raise LanguageModelError(
            f"empty content from {self.base_url} (finish={finish_reason!r}, "
            f"model={self.model!r}); try a higher max_tokens or a different model"
        )

    # ── Tool-use (Sprint 24) ───────────────────────────────────────────────

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> CompletionResponse:  # noqa: F821
        """Send a chat-completions request with tool-use enabled.

        Returns a CompletionResponse: either text content or a list of
        tool_calls the caller must execute and feed back. The OpenAI
        Chat Completions tools API drives this — most newer providers
        (OpenAI, Mistral, DeepSeek, Together, Groq, OpenRouter against
        a tools-capable model) support it. Older / smaller models may
        ignore the tools field; in that case we just get content.
        """
        from agent_core.skills.tools import CompletionResponse, ToolCall

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # System-prompt prefix message + caller-supplied messages
        full_messages = [{"role": "system", "content": system}, *messages]

        payload: dict = {
            "model": self.model,
            "messages": full_messages,
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": (temperature if temperature is not None else self.default_temperature),
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
            payload["tool_choice"] = "auto"

        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise LanguageModelError(
                f"timeout after {self.timeout}s talking to {self.base_url}"
            ) from e
        except httpx.HTTPError as e:
            raise LanguageModelError(f"network error to {self.base_url}: {e}") from e

        if resp.status_code >= 400:
            body = resp.text[:500]
            raise LanguageModelError(f"{self.base_url} returned {resp.status_code}: {body}")

        try:
            data = resp.json()
        except ValueError as e:
            raise LanguageModelError(f"{self.base_url} returned non-JSON: {resp.text[:200]}") from e

        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LanguageModelError(
                f"unexpected response shape from {self.base_url}: {data}"
            ) from e

        content = message.get("content")
        raw_tool_calls = message.get("tool_calls") or []

        tool_calls = []
        for tc in raw_tool_calls:
            try:
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments") or "{}"
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                tool_calls.append(
                    ToolCall(
                        id=tc.get("id") or "",
                        name=fn.get("name") or "",
                        arguments=args,
                    )
                )
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("malformed tool_call skipped: %s", e)
                continue

        return CompletionResponse(content=content, tool_calls=tool_calls)


# ── Factory ─────────────────────────────────────────────────────────────────


def language_model_from_settings(settings: object, secrets: object) -> object:
    """Build a LanguageModel implementation from settings + secrets.

    Returns one of:
      - ``StubLanguageModel`` if ``settings.llm.provider == "stub"``
      - ``OpenAICompatLanguageModel`` for ``openai_compat`` / ``ollama``
      - ``FallbackLanguageModel`` wrapping the above pair when
        ``settings.llm.fallback.provider`` is non-stub.

    The secrets store is consulted for the API key (under namespace
    ``llm``, key ``settings.llm.api_key_secret_key``). Missing key is OK
    for ``ollama`` provider; required for ``openai_compat``.
    """

    llm = settings.llm  # type: ignore[attr-defined]
    primary = _build_one_lm(
        provider=llm.provider,
        base_url=llm.base_url,
        model=llm.model,
        api_key_secret_key=llm.api_key_secret_key,
        max_tokens=llm.max_tokens,
        temperature=llm.temperature,
        timeout_seconds=llm.timeout_seconds,
        secrets=secrets,
    )

    # Fallback: optional secondary LM the primary falls back to on
    # LanguageModelError. Skip when fallback.provider == "stub" (the
    # default) — wrapping in a stub fallback would mask real failures
    # with canned text.
    fallback_cfg = getattr(llm, "fallback", None)
    if fallback_cfg is not None and fallback_cfg.provider != "stub":
        fallback = _build_one_lm(
            provider=fallback_cfg.provider,
            base_url=fallback_cfg.base_url,
            model=fallback_cfg.model,
            api_key_secret_key=fallback_cfg.api_key_secret_key,
            max_tokens=fallback_cfg.max_tokens,
            temperature=fallback_cfg.temperature,
            timeout_seconds=fallback_cfg.timeout_seconds,
            secrets=secrets,
        )
        from agent_core.skills.fallback import FallbackLanguageModel

        return FallbackLanguageModel(primary=primary, fallback=fallback)

    return primary


def _build_one_lm(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key_secret_key: str,
    max_tokens: int,
    temperature: float,
    timeout_seconds: float,
    secrets: object,
) -> object:
    """Construct a single LanguageModel from one set of LLM-shape fields."""
    from agent_core.skills.stubs import StubLanguageModel

    if provider == "stub":
        return StubLanguageModel(default="(stub-llm response)")

    api_key = secrets.get("llm", api_key_secret_key)  # type: ignore[attr-defined]
    if provider == "openai_compat" and not api_key:
        raise LanguageModelError(
            f"llm.provider={provider} but no API key in secrets store under "
            f"llm/{api_key_secret_key}. Set with: "
            f"`<product> secrets set llm.{api_key_secret_key}` "
            f"or env var AGENTCORE_LLM_{api_key_secret_key.upper()}=sk-..."
        )

    # Ollama default base_url override — be friendly to the common case.
    if provider == "ollama" and base_url == "https://api.openai.com/v1":
        base_url = "http://localhost:11434/v1"

    return OpenAICompatLanguageModel(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout_seconds,
        default_max_tokens=max_tokens,
        default_temperature=temperature,
    )


__all__ = [
    "LanguageModelError",
    "OpenAICompatLanguageModel",
    "language_model_from_settings",
]
