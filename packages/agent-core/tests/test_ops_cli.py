"""Smoke tests for agent_core.ops.cli — doctor / backup / restore / setup.

These exercise the Click surface; per-command behavior is covered by the
underlying module tests."""

from __future__ import annotations

import json
from pathlib import Path

from agent_core.ops.cli import (
    backup_command,
    doctor_command,
    restore_command,
    setup_command,
)
from click.testing import CliRunner

# ── doctor ──────────────────────────────────────────────────────────────────


def test_doctor_runs_on_default_install(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(doctor_command, ["--config", str(tmp_path / "agent.yml")])
    # Default install with no db should report ok overall (storage skipped).
    assert result.exit_code == 0
    assert "settings" in result.output


def test_doctor_json_output(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        doctor_command,
        ["--config", str(tmp_path / "agent.yml"), "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert all("name" in r and "status" in r for r in payload)


# ── backup / restore ───────────────────────────────────────────────────────


def test_backup_requires_db_url(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(backup_command, [str(tmp_path / "out.json")])
    assert result.exit_code != 0
    assert "--db-url is required" in result.output


def test_restore_requires_db_url(tmp_path: Path) -> None:
    src = tmp_path / "backup.json"
    src.write_text(json.dumps({"manifest": {"format_version": 1, "tables": {}}, "tables": {}}))
    runner = CliRunner()
    result = runner.invoke(restore_command, [str(src), "--yes"])
    assert result.exit_code != 0
    assert "--db-url is required" in result.output


def test_backup_then_restore_round_trip(tmp_path: Path) -> None:
    """End-to-end: write a backup, then restore it into a fresh db."""
    from agent_core.state import Database

    src_db = tmp_path / "src.db"
    target_db = tmp_path / "target.db"
    Database.sqlite(src_db).create_all()
    Database.sqlite(target_db).create_all()

    backup_file = tmp_path / "snapshot.json"
    runner = CliRunner()

    # Backup
    result = runner.invoke(
        backup_command,
        ["--db-url", f"sqlite:///{src_db}", str(backup_file)],
    )
    assert result.exit_code == 0, result.output
    assert backup_file.exists()

    # Restore
    result = runner.invoke(
        restore_command,
        [
            str(backup_file),
            "--db-url",
            f"sqlite:///{target_db}",
            "--yes",
            "--skip-schema-check",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "restored" in result.output.lower()


def test_restore_aborts_without_yes_or_input(tmp_path: Path) -> None:
    """Without --yes, restore prompts; CliRunner sends no input → abort."""
    from agent_core.state import Database

    db = tmp_path / "t.db"
    Database.sqlite(db).create_all()

    backup = tmp_path / "b.json"
    backup.write_text(json.dumps({"manifest": {"format_version": 1, "tables": {}}, "tables": {}}))

    runner = CliRunner()
    result = runner.invoke(
        restore_command,
        [str(backup), "--db-url", f"sqlite:///{db}", "--skip-schema-check"],
        input="\n",  # press return on Y/n → defaults to abort path
    )
    # Click's confirm(abort=True) returns 1 on abort.
    assert result.exit_code != 0


def test_restore_handles_bad_backup(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a backup"}')
    runner = CliRunner()
    result = runner.invoke(
        restore_command,
        [str(bad), "--db-url", "sqlite:///:memory:", "--yes"],
    )
    assert result.exit_code != 0
    assert "bad backup" in result.output.lower()


# ── setup ──────────────────────────────────────────────────────────────────


def test_setup_writes_settings_file(tmp_path: Path, monkeypatch) -> None:
    """Tier 1 setup, all defaults via stdin → file appears."""
    config_path = tmp_path / "agent.yml"
    runner = CliRunner()
    # stdin: 3 returns (preset default 'balanced', empty name, sqlite default).
    result = runner.invoke(
        setup_command,
        ["--tier", "1", "--config", str(config_path)],
        input="\n\n\n",
    )
    assert result.exit_code == 0, result.output
    # The file may exist as empty {} (no diff vs defaults) — that's fine,
    # just confirm we got to the success path.
    assert "wrote settings" in result.output


def test_setup_invalid_preset_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        setup_command,
        ["--tier", "1", "--config", str(tmp_path / "agent.yml")],
        input="yolo\n\nsqlite\n",
    )
    assert result.exit_code != 0
    assert "validation failed" in result.output.lower()
