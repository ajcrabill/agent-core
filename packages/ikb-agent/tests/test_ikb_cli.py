"""Smoke tests for ikb-agent CLI + defaults."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ikb_agent import (
    INSTANCE_NAME,
    config_dir,
    default_db_url,
    default_settings_path,
    state_dir,
)
from ikb_agent.cli import _redact_password, cli


# ── Defaults ───────────────────────────────────────────────────────────────


def test_instance_name_is_ikb_agent() -> None:
    assert INSTANCE_NAME == "ikb-agent"


def test_default_paths_under_xdg_dirs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("IKB_DB_URL", raising=False)
    assert state_dir() == tmp_path / "state" / "ikb-agent"
    assert config_dir() == tmp_path / "config" / "ikb-agent"
    assert default_settings_path() == tmp_path / "config" / "ikb-agent" / "agent.yml"


def test_default_db_url_is_postgres(monkeypatch) -> None:
    monkeypatch.delenv("IKB_DB_URL", raising=False)
    url = default_db_url()
    assert url.startswith("postgresql")
    assert "ikb_agent" in url


def test_db_url_overridable_via_env(monkeypatch) -> None:
    monkeypatch.setenv("IKB_DB_URL", "postgresql://x:y@host/db")
    assert default_db_url() == "postgresql://x:y@host/db"


# ── DSN redaction (for `ikb info` output) ──────────────────────────────────


def test_redact_password_hides_creds() -> None:
    redacted = _redact_password("postgresql://user:secret@host/db")
    assert "secret" not in redacted
    assert "user" in redacted
    assert "host" in redacted
    assert "***" in redacted


def test_redact_password_passes_dsn_without_credentials_through() -> None:
    bare = "postgresql:///ikb_agent?host=/tmp"
    assert _redact_password(bare) == bare


def test_redact_password_handles_url_without_password() -> None:
    """user-only (no password) URLs leave the user visible."""
    redacted = _redact_password("postgresql://user@host/db")
    assert "user" in redacted
    assert "***" not in redacted


# ── CLI surface ────────────────────────────────────────────────────────────


def test_cli_help_includes_ikb_brand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "ikb-agent" in result.output
    assert "knowledge base" in result.output


def test_cli_lists_expected_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("settings", "doctor", "backup", "restore", "setup", "info"):
        assert cmd in result.output


def test_cli_version_works() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.0.1" in result.output


def test_info_command_redacts_password(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("IKB_DB_URL", "postgresql://app:hunter2@db.example.com/ikb")
    runner = CliRunner()
    result = runner.invoke(cli, ["info"])
    assert result.exit_code == 0
    # Password must NOT appear in output (info is screenshot-prone)
    assert "hunter2" not in result.output
    # User + host info should still be visible
    assert "app" in result.output


def test_settings_subcommand_works(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    cfg = tmp_path / "agent.yml"
    result = runner.invoke(cli, ["settings", "--config", str(cfg), "show", "autonomy"])
    assert result.exit_code == 0
    assert "autonomy.default_policy" in result.output


def test_setup_creates_config_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    # Tier 1: 3 returns. ikb defaults to balanced too.
    result = runner.invoke(cli, ["setup", "--tier", "1"], input="\n\n\n")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "config" / "ikb-agent").exists()


# ── Re-exports ─────────────────────────────────────────────────────────────


def test_top_level_imports_work() -> None:
    from ikb_agent import (
        AgentSettings,
        Database,
        Notification,
        OpenBrainStore,
        SettingsManager,
        Urgency,
    )

    s = AgentSettings()
    # ikb-agent doesn't override defaults at the package level (config does that).
    assert s.autonomy.default_policy == "balanced"
    assert Notification(title="x", body="y", urgency=Urgency.warn).body == "y"
