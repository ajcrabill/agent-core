"""Tests for `agent ops init` — schema bootstrap + token generation."""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from agent_core.ops.cli import API_TOKEN_KEY, SECRETS_NAMESPACE, init_command
from agent_core.secrets import MemorySecretStore


# ── Fixture: monkeypatch the secrets store + a writable settings path ────


def _make_settings(tmp_path: Path, db_path: Path | None = None) -> Path:
    """Drop a minimal agent.yml at tmp_path/agent.yml pointing at db_path."""
    db_path = db_path or (tmp_path / "agent.db")
    cfg = tmp_path / "agent.yml"
    cfg.write_text(yaml.safe_dump({"storage": {"url": f"sqlite:///{db_path}"}}))
    return cfg


def _patch_default_store(monkeypatch) -> MemorySecretStore:
    """Patch the real default_store at its source so the local import inside
    init_command picks it up."""
    store = MemorySecretStore()
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: store)
    return store


# ── Tests ──────────────────────────────────────────────────────────────────


def test_init_bootstraps_schema_and_generates_token(tmp_path: Path, monkeypatch) -> None:
    cfg = _make_settings(tmp_path)
    store = _patch_default_store(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(init_command, ["--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "schema at head" in result.output
    assert "API token" in result.output
    # Token persisted into the (mocked) secret store
    token = store.get(SECRETS_NAMESPACE, API_TOKEN_KEY)
    assert token is not None
    assert len(token) > 20  # secrets.token_urlsafe(32) ≈ 43 chars
    # Token printed in the output too
    assert token in result.output


def test_init_idempotent_keeps_existing_token(tmp_path: Path, monkeypatch) -> None:
    cfg = _make_settings(tmp_path)
    store = _patch_default_store(monkeypatch)
    runner = CliRunner()

    runner.invoke(init_command, ["--config", str(cfg)])
    first_token = store.get(SECRETS_NAMESPACE, API_TOKEN_KEY)

    # Second run with no rotate flag should keep the same token
    result = runner.invoke(init_command, ["--config", str(cfg)])
    assert result.exit_code == 0
    assert "already present" in result.output
    assert store.get(SECRETS_NAMESPACE, API_TOKEN_KEY) == first_token


def test_init_rotate_token_replaces_existing(tmp_path: Path, monkeypatch) -> None:
    cfg = _make_settings(tmp_path)
    store = _patch_default_store(monkeypatch)
    runner = CliRunner()

    runner.invoke(init_command, ["--config", str(cfg)])
    first_token = store.get(SECRETS_NAMESPACE, API_TOKEN_KEY)

    result = runner.invoke(init_command, ["--config", str(cfg), "--rotate-token"])
    assert result.exit_code == 0
    new_token = store.get(SECRETS_NAMESPACE, API_TOKEN_KEY)
    assert new_token != first_token


def test_init_db_url_override_wins_over_settings(tmp_path: Path, monkeypatch) -> None:
    cfg = _make_settings(tmp_path)
    _patch_default_store(monkeypatch)
    override_db = tmp_path / "override.db"

    runner = CliRunner()
    result = runner.invoke(
        init_command,
        ["--config", str(cfg), "--db-url", f"sqlite:///{override_db}"],
    )
    assert result.exit_code == 0, result.output
    # Schema landed on the override path, not the settings path
    assert override_db.exists()


def test_init_creates_alembic_version_table(tmp_path: Path, monkeypatch) -> None:
    """Init must stamp alembic_version so future `alembic upgrade head` runs cleanly."""
    cfg = _make_settings(tmp_path)
    _patch_default_store(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(init_command, ["--config", str(cfg)])
    assert result.exit_code == 0

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "agent.db"))
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'")
    row = cur.fetchone()
    assert row is not None, "alembic_version table missing — future migrations will misbehave"
    cur.execute("SELECT version_num FROM alembic_version")
    version = cur.fetchone()[0]
    assert version  # non-empty


def test_init_creates_person_table(tmp_path: Path, monkeypatch) -> None:
    """Sanity check that Sprint 13a's migration ran (Person table exists)."""
    cfg = _make_settings(tmp_path)
    _patch_default_store(monkeypatch)
    runner = CliRunner()
    runner.invoke(init_command, ["--config", str(cfg)])

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "agent.db"))
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='person'")
    assert cur.fetchone() is not None


def test_init_with_no_db_url_fails(tmp_path: Path, monkeypatch) -> None:
    """If neither --db-url nor settings.storage.url resolves, refuse."""
    _patch_default_store(monkeypatch)
    cfg = tmp_path / "agent.yml"
    # Empty agent.yml — settings.storage.url falls back to schema default.
    # Override the schema default to None via env.
    cfg.write_text("storage:\n  url: ''\n")
    runner = CliRunner()
    result = runner.invoke(init_command, ["--config", str(cfg)])
    assert result.exit_code != 0
    assert "no db url" in result.output.lower()
