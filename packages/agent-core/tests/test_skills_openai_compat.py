"""Tests for OpenAICompatLanguageModel + language_model_from_settings."""

from __future__ import annotations

import json

import httpx
import pytest
from agent_core.secrets import MemorySecretStore
from agent_core.settings import AgentSettings
from agent_core.skills import (
    LanguageModelError,
    OpenAICompatLanguageModel,
    StubLanguageModel,
    language_model_from_settings,
)

# ── HTTP transport fixtures ────────────────────────────────────────────────


def _ok_response(content: str = "Hello from the model") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    )


def _mock_transport(handler) -> httpx.MockTransport:
    """Wrap a handler in a mock transport, install via monkeypatch."""
    return httpx.MockTransport(handler)


@pytest.fixture
def mock_post(monkeypatch):
    """Patch httpx.post to use a controllable handler.

    Returns a list — append your handler to slot 0:
        mock_post.append(lambda req: _ok_response("hi"))

    The fixture installs the handler at call time so each test can wire
    its own response."""
    calls: list = []

    def fake_post(url, *, json=None, headers=None, timeout=None, **kwargs):
        request = httpx.Request("POST", url, json=json, headers=headers)
        calls.append(request)
        if not handler_holder:
            return _ok_response()
        return handler_holder[0](request)

    handler_holder: list = []
    monkeypatch.setattr("httpx.post", fake_post)

    def set_handler(fn):
        handler_holder.clear()
        handler_holder.append(fn)

    return calls, set_handler


# ── OpenAICompatLanguageModel: happy path ─────────────────────────────────


def test_complete_returns_model_text(mock_post) -> None:
    calls, set_handler = mock_post
    set_handler(lambda req: _ok_response("Triaged: archive."))
    lm = OpenAICompatLanguageModel(
        base_url="https://api.example.com/v1",
        model="test-model",
        api_key="sk-test",
    )
    out = lm.complete(system="classify this", user="email body")
    assert out == "Triaged: archive."
    assert len(calls) == 1


def test_complete_sends_authorization_header(mock_post) -> None:
    calls, set_handler = mock_post
    set_handler(lambda req: _ok_response())
    lm = OpenAICompatLanguageModel(
        base_url="https://api.example.com/v1",
        model="m",
        api_key="sk-secret",
    )
    lm.complete(system="s", user="u")
    assert calls[0].headers["authorization"] == "Bearer sk-secret"


def test_complete_omits_authorization_when_no_key(mock_post) -> None:
    """Ollama doesn't need an API key; the header should be absent."""
    calls, set_handler = mock_post
    set_handler(lambda req: _ok_response())
    lm = OpenAICompatLanguageModel(
        base_url="http://localhost:11434/v1",
        model="llama3",
        api_key=None,
    )
    lm.complete(system="s", user="u")
    assert "authorization" not in calls[0].headers


def test_complete_sends_messages_in_correct_format(mock_post) -> None:
    calls, set_handler = mock_post
    set_handler(lambda req: _ok_response())
    lm = OpenAICompatLanguageModel(base_url="https://api.example.com/v1", model="m", api_key="k")
    lm.complete(system="SYS", user="USR")
    body = json.loads(calls[0].content)
    assert body["model"] == "m"
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]


def test_complete_strips_trailing_slash_in_base_url(mock_post) -> None:
    _, set_handler = mock_post
    set_handler(lambda req: _ok_response())
    lm = OpenAICompatLanguageModel(
        base_url="https://api.example.com/v1/",  # trailing slash
        model="m",
        api_key="k",
    )
    out = lm.complete(system="s", user="u")
    assert out  # didn't 404 from a doubled-slash URL


def test_max_tokens_and_temperature_override(mock_post) -> None:
    calls, set_handler = mock_post
    set_handler(lambda req: _ok_response())
    lm = OpenAICompatLanguageModel(
        base_url="https://x",
        model="m",
        api_key="k",
        default_max_tokens=999,
        default_temperature=0.7,
    )
    # No override → defaults
    lm.complete(system="s", user="u")
    body = json.loads(calls[0].content)
    assert body["max_tokens"] == 999
    assert body["temperature"] == 0.7

    # Override
    lm.complete(system="s", user="u", max_tokens=100, temperature=0.0)
    body = json.loads(calls[1].content)
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.0


def test_model_id_is_stable_for_telemetry(mock_post) -> None:
    lm = OpenAICompatLanguageModel(
        base_url="https://api.example.com/v1", model="gpt-4o-mini", api_key="k"
    )
    assert lm.model_id == "openai_compat:https://api.example.com/v1#gpt-4o-mini"


# ── OpenAICompatLanguageModel: failure modes ──────────────────────────────


def test_complete_raises_on_4xx(mock_post) -> None:
    _, set_handler = mock_post
    set_handler(lambda req: httpx.Response(401, json={"error": "invalid api key"}))
    lm = OpenAICompatLanguageModel(base_url="https://x", model="m", api_key="bad")
    with pytest.raises(LanguageModelError, match="401"):
        lm.complete(system="s", user="u")


def test_complete_raises_on_5xx(mock_post) -> None:
    _, set_handler = mock_post
    set_handler(lambda req: httpx.Response(503, text="service unavailable"))
    lm = OpenAICompatLanguageModel(base_url="https://x", model="m", api_key="k")
    with pytest.raises(LanguageModelError, match="503"):
        lm.complete(system="s", user="u")


def test_complete_raises_on_timeout(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr("httpx.post", boom)
    lm = OpenAICompatLanguageModel(base_url="https://x", model="m", api_key="k", timeout=0.1)
    with pytest.raises(LanguageModelError, match="timeout"):
        lm.complete(system="s", user="u")


def test_complete_raises_on_network_error(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.post", boom)
    lm = OpenAICompatLanguageModel(base_url="https://x", model="m", api_key="k")
    with pytest.raises(LanguageModelError, match="network error"):
        lm.complete(system="s", user="u")


def test_complete_raises_on_non_json_response(mock_post) -> None:
    _, set_handler = mock_post
    set_handler(lambda req: httpx.Response(200, text="<html>not json</html>"))
    lm = OpenAICompatLanguageModel(base_url="https://x", model="m", api_key="k")
    with pytest.raises(LanguageModelError, match="non-JSON"):
        lm.complete(system="s", user="u")


def test_complete_raises_on_unexpected_response_shape(mock_post) -> None:
    _, set_handler = mock_post
    set_handler(lambda req: httpx.Response(200, json={"weird": "shape"}))
    lm = OpenAICompatLanguageModel(base_url="https://x", model="m", api_key="k")
    with pytest.raises(LanguageModelError, match="unexpected response shape"):
        lm.complete(system="s", user="u")


# ── language_model_from_settings ──────────────────────────────────────────


def test_factory_returns_stub_for_stub_provider() -> None:
    s = AgentSettings(llm={"provider": "stub"})  # type: ignore[arg-type]
    lm = language_model_from_settings(s, MemorySecretStore())
    assert isinstance(lm, StubLanguageModel)


def test_factory_returns_openai_compat_with_key() -> None:
    s = AgentSettings(
        llm={  # type: ignore[arg-type]
            "provider": "openai_compat",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key_secret_key": "openai_api_key",
        }
    )
    secrets = MemorySecretStore()
    secrets.set("llm", "openai_api_key", "sk-real-key")
    lm = language_model_from_settings(s, secrets)
    assert isinstance(lm, OpenAICompatLanguageModel)
    assert lm.api_key == "sk-real-key"
    assert lm.model == "gpt-4o-mini"


def test_factory_raises_when_openai_compat_has_no_key() -> None:
    s = AgentSettings(
        llm={  # type: ignore[arg-type]
            "provider": "openai_compat",
            "api_key_secret_key": "openai_api_key",
        }
    )
    with pytest.raises(LanguageModelError, match="API key"):
        language_model_from_settings(s, MemorySecretStore())


def test_factory_ollama_works_without_key() -> None:
    """Ollama doesn't require auth — factory tolerates missing key."""
    s = AgentSettings(llm={"provider": "ollama", "model": "llama3.2"})  # type: ignore[arg-type]
    lm = language_model_from_settings(s, MemorySecretStore())
    assert isinstance(lm, OpenAICompatLanguageModel)
    assert lm.api_key is None


def test_factory_ollama_overrides_base_url_when_default() -> None:
    """If user picks ollama but leaves the OpenAI default base_url, switch
    to localhost. Ergonomic — `--llm-provider ollama` alone should work."""
    s = AgentSettings(
        llm={"provider": "ollama", "base_url": "https://api.openai.com/v1"}  # type: ignore[arg-type]
    )
    lm = language_model_from_settings(s, MemorySecretStore())
    assert lm.base_url == "http://localhost:11434/v1"


def test_factory_ollama_respects_explicit_base_url() -> None:
    s = AgentSettings(
        llm={  # type: ignore[arg-type]
            "provider": "ollama",
            "base_url": "http://my-ollama-server:11434/v1",
        }
    )
    lm = language_model_from_settings(s, MemorySecretStore())
    assert lm.base_url == "http://my-ollama-server:11434/v1"


def test_factory_passes_through_timeout_and_token_limits() -> None:
    s = AgentSettings(
        llm={  # type: ignore[arg-type]
            "provider": "openai_compat",
            "max_tokens": 500,
            "temperature": 0.42,
            "timeout_seconds": 5.0,
        }
    )
    secrets = MemorySecretStore()
    secrets.set("llm", "openai_api_key", "k")
    lm = language_model_from_settings(s, secrets)
    assert lm.timeout == 5.0
    assert lm.default_max_tokens == 500
    assert lm.default_temperature == pytest.approx(0.42)
