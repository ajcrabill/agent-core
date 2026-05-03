"""Smoke tests for dcos-agent CLI + defaults."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from dcos_agent import (
    INSTANCE_NAME,
    config_dir,
    default_db_path,
    default_db_url,
    default_settings_path,
    state_dir,
)
from dcos_agent.cli import cli


# ── Defaults ───────────────────────────────────────────────────────────────


def test_instance_name_is_dcos_agent() -> None:
    assert INSTANCE_NAME == "dcos-agent"


def test_default_paths_under_xdg_dirs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert state_dir() == tmp_path / "state" / "dcos-agent"
    assert config_dir() == tmp_path / "config" / "dcos-agent"
    assert default_db_path() == tmp_path / "state" / "dcos-agent" / "agent.db"
    assert default_settings_path() == tmp_path / "config" / "dcos-agent" / "agent.yml"


def test_default_db_url_is_sqlite_for_default_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    url = default_db_url()
    assert url.startswith("sqlite:///")
    assert "dcos-agent" in url


# ── CLI surface ────────────────────────────────────────────────────────────


def test_cli_help_includes_dcos_brand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "dcos-agent" in result.output
    assert "chief of staff" in result.output


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
    # Click version output: "dcos, version X.Y.Z"
    assert "0.0.1" in result.output


def test_info_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    result = runner.invoke(cli, ["info"])
    assert result.exit_code == 0
    assert "dcos-agent" in result.output
    # Rich may wrap long paths — assert the key labels rather than full paths.
    assert "version" in result.output
    assert "config" in result.output
    assert "db" in result.output


def test_doctor_runs_with_dcos_defaults(monkeypatch, tmp_path: Path) -> None:
    """`dcos doctor` should run cleanly even with no install (everything skipped)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0  # default install with no db should pass
    assert "settings" in result.output


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
    # Tier 1: 3 returns (preset default, empty name, sqlite default)
    result = runner.invoke(cli, ["setup", "--tier", "1"], input="\n\n\n")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "config" / "dcos-agent").exists()


# ── Re-exports from agent-core ─────────────────────────────────────────────


def test_top_level_imports_work() -> None:
    """The dcos_agent namespace re-exports the most common agent-core types."""
    from dcos_agent import (
        AgentSettings,
        Database,
        Notification,
        NotificationDispatcher,
        OpenBrainStore,
        SettingsManager,
        Urgency,
    )

    s = AgentSettings()
    assert s.autonomy.default_policy == "balanced"
    assert Notification(title="x", body="y", urgency=Urgency.warn).title == "x"
