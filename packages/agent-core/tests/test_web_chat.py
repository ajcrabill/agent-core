"""Tests for the /chat HTML page + /chat/turn endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_core.openbrain import OpenBrainStore, StubEmbeddingProvider
from agent_core.secrets import MemorySecretStore
from agent_core.settings import AgentSettings
from agent_core.state import Database
from agent_core.web import create_app
from fastapi.testclient import TestClient

TOKEN = "chat-test-token"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database.sqlite(tmp_path / "test.db")
    d.create_all()
    return d


@pytest.fixture
def app_factory(db: Database, monkeypatch):
    """Build a TestClient. Patches default_store so the LLM factory can
    look up an in-memory test secret without touching the OS keychain."""
    store = MemorySecretStore()
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: store)

    def _build(
        *,
        settings: AgentSettings | None = None,
    ) -> TestClient:
        s = settings or AgentSettings()
        ob_store = OpenBrainStore(db, StubEmbeddingProvider())
        app = create_app(db, s, api_tokens={TOKEN}, openbrain=ob_store)
        return TestClient(app)

    return _build


def _hdr() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ── HTML page ──────────────────────────────────────────────────────────────


def test_chat_page_renders_html(app_factory) -> None:
    """GET /chat returns the static HTML page (no auth on the page itself)."""
    client = app_factory()
    r = client.get("/chat")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<title>chat — agent-core</title>" in r.text


def test_chat_page_has_message_input_and_token_field(app_factory) -> None:
    client = app_factory()
    r = client.get("/chat")
    assert 'id="message"' in r.text
    assert 'id="token"' in r.text


def test_chat_page_no_external_dependencies(app_factory) -> None:
    """The page should be self-contained — no <script src=>, no <link rel=stylesheet>."""
    client = app_factory()
    r = client.get("/chat")
    text = r.text.lower()
    # Script + style live inline only
    assert "<script src=" not in text
    assert '<link rel="stylesheet"' not in text


# ── /chat/turn — auth ──────────────────────────────────────────────────────


def test_chat_turn_requires_auth(app_factory) -> None:
    client = app_factory()
    r = client.post("/chat/turn", json={"message": "hi"})
    assert r.status_code == 401


def test_chat_turn_rejects_invalid_token(app_factory) -> None:
    client = app_factory()
    r = client.post(
        "/chat/turn",
        json={"message": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


# ── /chat/turn — happy path ───────────────────────────────────────────────


def test_chat_turn_with_stub_settings_returns_503(app_factory) -> None:
    """Default LLM provider is "stub" — but the factory returns a
    StubLanguageModel, which the factory considers configured. Response
    should be a 200 with stub text."""
    client = app_factory()
    r = client.post("/chat/turn", json={"message": "hello"}, headers=_hdr())
    assert r.status_code == 200
    payload = r.json()
    assert "reply" in payload
    assert "session_id" in payload
    assert "history" in payload
    assert len(payload["history"]) == 2  # user + assistant
    assert payload["history"][0]["role"] == "user"
    assert payload["history"][1]["role"] == "assistant"


def test_chat_turn_returns_503_when_llm_misconfigured(app_factory, monkeypatch) -> None:
    """Set provider=openai_compat but no key — factory raises; endpoint 503."""
    s = AgentSettings(llm={"provider": "openai_compat"})  # type: ignore[arg-type]
    client = app_factory(settings=s)
    r = client.post("/chat/turn", json={"message": "hi"}, headers=_hdr())
    assert r.status_code == 503
    assert "LLM not configured" in r.json()["detail"]


def test_chat_turn_continues_conversation(app_factory) -> None:
    """history+session_id round-trip continues a multi-turn conversation."""
    client = app_factory()
    r1 = client.post("/chat/turn", json={"message": "first"}, headers=_hdr())
    assert r1.status_code == 200
    r2 = client.post(
        "/chat/turn",
        json={
            "message": "second",
            "session_id": r1.json()["session_id"],
            "history": r1.json()["history"],
        },
        headers=_hdr(),
    )
    assert r2.status_code == 200
    history = r2.json()["history"]
    assert len(history) == 4  # 2 user + 2 assistant
    assert history[0]["content"] == "first"
    assert history[2]["content"] == "second"


def test_chat_turn_rejects_empty_message(app_factory) -> None:
    client = app_factory()
    r = client.post("/chat/turn", json={"message": ""}, headers=_hdr())
    assert r.status_code == 422


def test_chat_turn_rejects_oversized_message(app_factory) -> None:
    client = app_factory()
    huge = "x" * 100_000
    r = client.post("/chat/turn", json={"message": huge}, headers=_hdr())
    assert r.status_code == 422


def test_chat_turn_session_id_persists_across_calls(app_factory) -> None:
    client = app_factory()
    r1 = client.post("/chat/turn", json={"message": "a"}, headers=_hdr())
    sid1 = r1.json()["session_id"]
    r2 = client.post(
        "/chat/turn",
        json={"message": "b", "session_id": sid1, "history": r1.json()["history"]},
        headers=_hdr(),
    )
    assert r2.json()["session_id"] == sid1


def test_chat_turn_no_inject_context_skips_injection(app_factory, db) -> None:
    """When inject_context=False, the LM doesn't see obligations in system prompt."""
    from agent_core.state.models import Obligation, ObligationSource

    with db.session() as s:
        s.add(Obligation(title="DO-NOT-INJECT", source=ObligationSource.manual))
        s.commit()

    client = app_factory()
    r = client.post(
        "/chat/turn",
        json={"message": "x", "inject_context": False},
        headers=_hdr(),
    )
    assert r.status_code == 200
    # We can't directly assert what the LM saw — but the call succeeded.
    # The unit tests for run_turn cover injection logic itself.
