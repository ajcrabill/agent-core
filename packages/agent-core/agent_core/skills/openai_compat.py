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
    ) -> str:
        """Send a chat-completions request, return the model's text reply.

        Raises ``LanguageModelError`` on any failure — network, status,
        empty content, malformed JSON. Skills decide whether to retry.
        """
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": (
                temperature if temperature is not None else self.default_temperature
            ),
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
            raise LanguageModelError(
                f"{self.base_url} returned {resp.status_code}: {body}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise LanguageModelError(
                f"{self.base_url} returned non-JSON: {resp.text[:200]}"
            ) from e

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LanguageModelError(
                f"unexpected response shape from {self.base_url}: {data}"
            ) from e

        if not isinstance(content, str):
            raise LanguageModelError(
                f"expected string content, got {type(content).__name__}: {content!r}"
            )

        return content


# ── Factory ─────────────────────────────────────────────────────────────────


def language_model_from_settings(settings: object, secrets: object) -> object:
    """Build a LanguageModel implementation from settings + secrets.

    Returns one of:
      - ``StubLanguageModel`` if ``settings.llm.provider == "stub"``
      - ``OpenAICompatLanguageModel`` for ``openai_compat`` / ``ollama``

    The secrets store is consulted for the API key (under namespace
    ``llm``, key ``settings.llm.api_key_secret_key``). Missing key is OK
    for ``ollama`` provider; required for ``openai_compat``.
    """
    from agent_core.skills.stubs import StubLanguageModel

    llm = settings.llm  # type: ignore[attr-defined]

    if llm.provider == "stub":
        return StubLanguageModel(default="(stub-llm response)")

    api_key = secrets.get("llm", llm.api_key_secret_key)  # type: ignore[attr-defined]
    if llm.provider == "openai_compat" and not api_key:
        raise LanguageModelError(
            f"llm.provider={llm.provider} but no API key in secrets store under "
            f"llm/{llm.api_key_secret_key}. Set with: "
            f"AGENTCORE_LLM_{llm.api_key_secret_key.upper()}=sk-... "
            f"or via `agent settings llm api-key set`."
        )

    # Ollama default base_url override — be friendly to the common case.
    if llm.provider == "ollama" and llm.base_url == "https://api.openai.com/v1":
        base_url = "http://localhost:11434/v1"
    else:
        base_url = llm.base_url

    return OpenAICompatLanguageModel(
        base_url=base_url,
        model=llm.model,
        api_key=api_key,
        timeout=llm.timeout_seconds,
        default_max_tokens=llm.max_tokens,
        default_temperature=llm.temperature,
    )


__all__ = [
    "LanguageModelError",
    "OpenAICompatLanguageModel",
    "language_model_from_settings",
]
