"""Tests for `agent web serve` — token resolution + app construction.

We don't actually start uvicorn — that would block the test. Instead we
monkeypatch ``uvicorn.run`` and assert the command builds the right app
with the right token, then short-circuits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from agent_core.ops.cli import API_TOKEN_KEY, SECRETS_NAMESPACE
from agent_core.secrets import MemorySecretStore
from agent_core.web.cli import serve_command
from click.testing import CliRunner


def _make_settings(tmp_path: Path) -> Path:
    cfg = tmp_path / "agent.yml"
    cfg.write_text(yaml.safe_dump({"storage": {"url": f"sqlite:///{tmp_path / 'agent.db'}"}}))
    return cfg


@pytest.fixture
def patched_uvicorn(monkeypatch):
    """Capture the uvicorn.run call without actually starting a server."""
    calls: list[dict] = []

    def fake_run(app, *, host, port, reload, log_level):
        calls.append({"app": app, "host": host, "port": port, "reload": reload})

    # Patch where uvicorn.run is looked up (web.cli imports it at call time)
    monkeypatch.setattr("uvicorn.run", fake_run)
    return calls


@pytest.fixture
def memory_store(monkeypatch) -> MemorySecretStore:
    """Patch the real default_store; web.cli imports it at module level
    but the local indirection still picks up the source-level patch."""
    store = MemorySecretStore()
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: store)
    monkeypatch.setattr("agent_core.web.cli.default_store", lambda: store)
    return store


# ── Tests ──────────────────────────────────────────────────────────────────


def test_serve_loads_token_from_secret_store(tmp_path: Path, memory_store, patched_uvicorn) -> None:
    cfg = _make_settings(tmp_path)
    memory_store.set(SECRETS_NAMESPACE, API_TOKEN_KEY, "test-token-from-store-123")
    # Bootstrap minimal schema so the API can build
    from agent_core.state.db import Database

    Database(f"sqlite:///{tmp_path / 'agent.db'}").create_all()

    runner = CliRunner()
    result = runner.invoke(
        serve_command,
        ["--config", str(cfg), "--port", "0"],
    )
    assert result.exit_code == 0, result.output
    assert "test-token-from-store-123" in result.output
    assert len(patched_uvicorn) == 1
    assert patched_uvicorn[0]["port"] == 0


def test_serve_explicit_token_wins_over_store(
    tmp_path: Path, memory_store, patched_uvicorn
) -> None:
    cfg = _make_settings(tmp_path)
    memory_store.set(SECRETS_NAMESPACE, API_TOKEN_KEY, "from-store")
    from agent_core.state.db import Database

    Database(f"sqlite:///{tmp_path / 'agent.db'}").create_all()

    runner = CliRunner()
    result = runner.invoke(
        serve_command,
        ["--config", str(cfg), "--port", "0", "--token", "from-cli"],
    )
    assert result.exit_code == 0, result.output
    assert "from-cli" in result.output
    assert "from-store" not in result.output


def test_serve_fails_when_no_token_anywhere(tmp_path: Path, memory_store, patched_uvicorn) -> None:
    """Fail closed — if neither the secrets store nor --token has a token,
    refuse to start. (A serve command running without auth would silently
    accept all callers.)"""
    cfg = _make_settings(tmp_path)
    # memory_store is empty
    runner = CliRunner()
    result = runner.invoke(serve_command, ["--config", str(cfg), "--port", "0"])
    assert result.exit_code != 0
    assert "no api token" in result.output.lower()
    assert patched_uvicorn == []


def test_serve_uses_settings_db_url_by_default(
    tmp_path: Path, memory_store, patched_uvicorn
) -> None:
    cfg = _make_settings(tmp_path)
    memory_store.set(SECRETS_NAMESPACE, API_TOKEN_KEY, "x")
    from agent_core.state.db import Database

    Database(f"sqlite:///{tmp_path / 'agent.db'}").create_all()

    runner = CliRunner()
    result = runner.invoke(serve_command, ["--config", str(cfg), "--port", "0"])
    assert result.exit_code == 0, result.output


def test_serve_passes_host_and_port_through(tmp_path: Path, memory_store, patched_uvicorn) -> None:
    cfg = _make_settings(tmp_path)
    memory_store.set(SECRETS_NAMESPACE, API_TOKEN_KEY, "x")
    from agent_core.state.db import Database

    Database(f"sqlite:///{tmp_path / 'agent.db'}").create_all()

    runner = CliRunner()
    result = runner.invoke(
        serve_command,
        ["--config", str(cfg), "--host", "0.0.0.0", "--port", "9999"],
    )
    assert result.exit_code == 0, result.output
    assert patched_uvicorn[0]["host"] == "0.0.0.0"
    assert patched_uvicorn[0]["port"] == 9999
