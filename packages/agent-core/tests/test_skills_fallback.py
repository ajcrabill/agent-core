"""FallbackLanguageModel tests.

Covers the wrapper's behavior in three regimes:
  1. Primary succeeds → fallback never invoked
  2. Primary raises LanguageModelError → fallback invoked, result returned
  3. Other exceptions from primary propagate (no silent masking of bugs)

Plus the wiring: language_model_from_settings honors llm.fallback when
provider != stub.
"""

from __future__ import annotations

import pytest

from agent_core.settings import AgentSettings
from agent_core.skills import (
    CompletionResponse,
    FallbackLanguageModel,
    LanguageModelError,
    StubLanguageModel,
    ToolCall,
    language_model_from_settings,
)


# ── helpers ─────────────────────────────────────────────────────────────────


class _AlwaysFails:
    """Raises LanguageModelError on every call. Both .complete and
    .complete_with_tools are present so we can test both paths."""

    model_id = "always-fails"

    def __init__(self):
        self.complete_calls = 0
        self.complete_with_tools_calls = 0

    def complete(self, *, system, user, max_tokens=None, temperature=None):
        self.complete_calls += 1
        raise LanguageModelError("primary down")

    def complete_with_tools(self, *, system, messages, tools, max_tokens=None, temperature=None):
        self.complete_with_tools_calls += 1
        raise LanguageModelError("primary down (tools)")


class _AlwaysSucceeds:
    """Returns canned content. Used as the fallback in success-path tests."""

    model_id = "always-succeeds"

    def __init__(self, text="fallback text"):
        self.text = text
        self.complete_calls = 0
        self.complete_with_tools_calls = 0

    def complete(self, *, system, user, max_tokens=None, temperature=None):
        self.complete_calls += 1
        return self.text

    def complete_with_tools(self, *, system, messages, tools, max_tokens=None, temperature=None):
        self.complete_with_tools_calls += 1
        return CompletionResponse(content=self.text, tool_calls=[])


class _RaisesNonLMError:
    """Raises a non-LM exception type to verify it propagates unwrapped."""

    model_id = "boom"

    def complete(self, **kwargs):
        raise ValueError("programmer error, not an LM failure")

    def complete_with_tools(self, **kwargs):
        raise ValueError("programmer error in tools path")


class _NoToolsLM:
    """Plain-complete-only LM (mimics older / simpler models)."""

    model_id = "plain-only"

    def __init__(self, text="plain"):
        self.text = text
        self.complete_calls = 0

    def complete(self, *, system, user, max_tokens=None, temperature=None):
        self.complete_calls += 1
        return self.text


# ── complete() ──────────────────────────────────────────────────────────────


def test_fallback_primary_succeeds_does_not_call_fallback():
    primary = _AlwaysSucceeds("primary text")
    fallback = _AlwaysSucceeds("fallback text")
    lm = FallbackLanguageModel(primary=primary, fallback=fallback)

    out = lm.complete(system="x", user="y")

    assert out == "primary text"
    assert primary.complete_calls == 1
    assert fallback.complete_calls == 0


def test_fallback_primary_raises_lm_error_invokes_fallback():
    primary = _AlwaysFails()
    fallback = _AlwaysSucceeds("rescued")
    lm = FallbackLanguageModel(primary=primary, fallback=fallback)

    out = lm.complete(system="x", user="y")

    assert out == "rescued"
    assert primary.complete_calls == 1
    assert fallback.complete_calls == 1


def test_fallback_propagates_non_lm_exceptions_unwrapped():
    """ValueError from primary should NOT trigger fallback — that path is
    for transport/network failures, not bugs."""
    primary = _RaisesNonLMError()
    fallback = _AlwaysSucceeds()
    lm = FallbackLanguageModel(primary=primary, fallback=fallback)

    with pytest.raises(ValueError, match="programmer error"):
        lm.complete(system="x", user="y")
    assert fallback.complete_calls == 0


def test_fallback_chain_does_not_recurse_when_fallback_also_fails():
    """If both fail, the fallback's exception propagates raw — there's
    no third tier."""
    primary = _AlwaysFails()
    fallback = _AlwaysFails()  # second one also fails
    lm = FallbackLanguageModel(primary=primary, fallback=fallback)

    with pytest.raises(LanguageModelError, match="primary down"):
        lm.complete(system="x", user="y")
    # Both got called once
    assert primary.complete_calls == 1
    assert fallback.complete_calls == 1


# ── complete_with_tools() ───────────────────────────────────────────────────


def test_fallback_tools_primary_succeeds():
    primary = _AlwaysSucceeds("primary tools")
    fallback = _AlwaysSucceeds("fallback tools")
    lm = FallbackLanguageModel(primary=primary, fallback=fallback)

    resp = lm.complete_with_tools(system="x", messages=[], tools=[])

    assert resp.content == "primary tools"
    assert primary.complete_with_tools_calls == 1
    assert fallback.complete_with_tools_calls == 0


def test_fallback_tools_primary_fails_invokes_fallback_tools():
    primary = _AlwaysFails()
    fallback = _AlwaysSucceeds("rescued via tools")
    lm = FallbackLanguageModel(primary=primary, fallback=fallback)

    resp = lm.complete_with_tools(system="x", messages=[], tools=[])

    assert resp.content == "rescued via tools"
    assert primary.complete_with_tools_calls == 1
    assert fallback.complete_with_tools_calls == 1


def test_fallback_tools_degrades_to_plain_when_fallback_lacks_tools():
    primary = _AlwaysFails()
    fallback = _NoToolsLM("plain rescue")
    lm = FallbackLanguageModel(primary=primary, fallback=fallback)

    resp = lm.complete_with_tools(
        system="x",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )

    assert resp == "plain rescue"
    assert primary.complete_with_tools_calls == 1
    assert fallback.complete_calls == 1


def test_fallback_tools_primary_lacks_tools_uses_primary_plain():
    """When primary has no complete_with_tools, the wrapper sends it
    through primary's plain complete — it doesn't auto-promote to
    fallback's tool support."""
    primary = _NoToolsLM("primary plain")
    fallback = _AlwaysSucceeds("fallback shouldn't run")
    lm = FallbackLanguageModel(primary=primary, fallback=fallback)

    out = lm.complete_with_tools(
        system="x",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )

    assert out == "primary plain"
    assert primary.complete_calls == 1
    assert fallback.complete_with_tools_calls == 0
    assert fallback.complete_calls == 0


# ── model_id reporting ─────────────────────────────────────────────────────


def test_fallback_model_id_combines_both_legs():
    primary = _AlwaysSucceeds()
    fallback = _AlwaysSucceeds()
    lm = FallbackLanguageModel(primary=primary, fallback=fallback)
    assert "always-succeeds" in lm.model_id
    assert "→" in lm.model_id


# ── language_model_from_settings wiring ────────────────────────────────────


class _Secrets:
    def __init__(self, store=None):
        self._store = store or {}

    def get(self, ns, key):
        return self._store.get((ns, key))


def test_lmfs_returns_bare_lm_when_fallback_provider_stub():
    """Default fallback.provider='stub' → no FallbackLanguageModel wrap."""
    s = AgentSettings()
    s.llm.provider = "ollama"
    s.llm.base_url = "http://localhost:11434/v1"
    s.llm.model = "x"
    # fallback.provider is "stub" by default

    lm = language_model_from_settings(s, _Secrets())
    assert not isinstance(lm, FallbackLanguageModel)


def test_lmfs_wraps_in_fallback_when_fallback_configured():
    s = AgentSettings()
    s.llm.provider = "ollama"
    s.llm.base_url = "http://localhost:11434/v1"
    s.llm.model = "gemma4:26b"
    s.llm.fallback.provider = "openai_compat"
    s.llm.fallback.base_url = "https://api.deepseek.com/v1"
    s.llm.fallback.model = "deepseek-chat"

    secrets = _Secrets({("llm", "deepseek_api_key"): "sk-deepseek-xxx"})
    lm = language_model_from_settings(s, secrets)

    assert isinstance(lm, FallbackLanguageModel)
    # Both legs are real OpenAICompat models (one Ollama, one DeepSeek)
    assert lm.primary.base_url == "http://localhost:11434/v1"
    assert lm.primary.model == "gemma4:26b"
    assert lm.fallback.base_url == "https://api.deepseek.com/v1"
    assert lm.fallback.model == "deepseek-chat"
    assert lm.fallback.api_key == "sk-deepseek-xxx"


def test_lmfs_fallback_missing_api_key_raises_at_build_time():
    """If fallback.provider=openai_compat but the secret isn't set,
    we surface the error at construction (clearer than a runtime
    fallback-of-fallback failure)."""
    s = AgentSettings()
    s.llm.provider = "stub"  # primary works
    s.llm.fallback.provider = "openai_compat"
    s.llm.fallback.base_url = "https://api.deepseek.com/v1"
    s.llm.fallback.model = "deepseek-chat"

    secrets = _Secrets({})  # no key
    with pytest.raises(LanguageModelError, match="deepseek_api_key"):
        language_model_from_settings(s, secrets)
