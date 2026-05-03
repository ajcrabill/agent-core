"""Tests for agent_core.migrations.from_esby_install."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from agent_core.migrations import (
    EsbyInstallMigration,
    esby_to_backup_payload,
    migrate_esby_install,
)
from agent_core.migrations.cli import migrate_group
from agent_core.ops import restore_backup
from agent_core.state import AutonomyOverride, Database, LearningRule, Person, Thought
from sqlmodel import select


# ── Synthetic install fixture ──────────────────────────────────────────────


def _build_install(tmp_path: Path) -> Path:
    """Build a synthetic Esby install dir with a populated sqlite + a few configs."""
    root = tmp_path / "installed-chief-of-staff"
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    config_dir = root / "config"
    config_dir.mkdir(parents=True)

    db_path = state_dir / "chief_of_staff.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            organization TEXT,
            role TEXT,
            stakeholder_class TEXT NOT NULL DEFAULT 'unknown_external',
            autonomy_override TEXT DEFAULT 'inherit',
            relationship_intensity INTEGER,
            tone_profile TEXT,
            response_sla TEXT,
            never_autonomous_send INTEGER NOT NULL DEFAULT 0,
            sensitive_memory_flag INTEGER NOT NULL DEFAULT 0,
            notes_path TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE policy_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL,
            match_json TEXT NOT NULL,
            decision TEXT NOT NULL,
            confidence_threshold REAL,
            novelty_threshold REAL,
            reversible_only INTEGER NOT NULL DEFAULT 0,
            priority_limit TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        );
        INSERT INTO people (name, stakeholder_class, relationship_intensity, never_autonomous_send) VALUES
            ('Robyne', 'key_internal', 5, 0),
            ('Charlotte', 'family_member', 3, 1),
            ('Jessica', 'principal_client', 4, 1);
        INSERT INTO policy_rules (rule_name, scope, match_json, decision, confidence_threshold, reversible_only, enabled) VALUES
            ('global_no_send_principal_client', 'global',
             '{"stakeholder_class":"principal_client","action_type":"send_email"}',
             'approval_required', 0.9, 0, 1),
            ('internal_low_priority_reply', 'workflow_slice',
             '{"stakeholder_class":["internal","key_internal"],"workflow_name":"email_reply","priority":"low"}',
             'draft_only', 0.65, 1, 1),
            ('disabled_rule', 'global', '{}', 'approval_required', 0.9, 0, 0);
        """
    )
    conn.commit()
    conn.close()

    # Configs
    (config_dir / "preferences.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "preferences": {"autonomy_bias": {"type": "ternary", "default": "option_a"}},
            }
        )
    )
    (config_dir / "autonomy-matrix.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default_initial_state": {"all_workflow_slices": "draft_only_or_approval_required"},
                "never_autonomous_send_default": ["principal_client", "family_member"],
            }
        )
    )
    (config_dir / "stakeholder_classes.yaml").write_text(
        "version: 1\nstakeholder_classes:\n  self:\n    description: principal\n"
    )

    (root / "setup-report.md").write_text(
        "# Setup Report\n\n## Scaffold results\n- Repo root: /Users/esby/x\n- DB: state/x.sqlite\n"
    )
    return root


# ── People extraction ──────────────────────────────────────────────────────


def test_migration_extracts_people_rows(tmp_path: Path) -> None:
    state = migrate_esby_install(_build_install(tmp_path))
    names = {p.name for p in state.people}
    assert names == {"Robyne", "Charlotte", "Jessica"}


def test_migration_preserves_person_fields(tmp_path: Path) -> None:
    state = migrate_esby_install(_build_install(tmp_path))
    by_name = {p.name: p for p in state.people}
    assert by_name["Robyne"].stakeholder_class == "key_internal"
    assert by_name["Robyne"].relationship_intensity == 5
    assert by_name["Charlotte"].never_autonomous_send is True
    assert by_name["Jessica"].stakeholder_class == "principal_client"


def test_migration_records_implicit_autonomy_class_in_metadata(tmp_path: Path) -> None:
    """Esby's autonomy-matrix marks principal_client/family_member as implicit
    no-autonomous-send classes — captured in metadata even when the row's flag is False."""
    state = migrate_esby_install(_build_install(tmp_path))
    by_name = {p.name: p for p in state.people}
    assert by_name["Charlotte"].metadata_json["implicit_no_autonomous_send_class"] is True
    assert by_name["Robyne"].metadata_json["implicit_no_autonomous_send_class"] is False


# ── Policy → LearningRule ─────────────────────────────────────────────────


def test_migration_translates_enabled_policy_rules_only(tmp_path: Path) -> None:
    """``disabled_rule`` (enabled=0) must NOT make it into the LearningRules."""
    state = migrate_esby_install(_build_install(tmp_path))
    sources = {r.source for r in state.learning_rules}
    assert "esby-policy:global_no_send_principal_client" in sources
    assert "esby-policy:internal_low_priority_reply" in sources
    assert "esby-policy:disabled_rule" not in sources


def test_send_email_rule_tagged_for_email_composer(tmp_path: Path) -> None:
    state = migrate_esby_install(_build_install(tmp_path))
    by_source = {r.source: r for r in state.learning_rules}
    rule = by_source["esby-policy:global_no_send_principal_client"]
    assert "email-composer" in rule.skill_tags
    assert "principal_client" in rule.correction
    assert "approval" in rule.correction.lower()


def test_workflow_email_reply_rule_tagged_for_email_composer(tmp_path: Path) -> None:
    state = migrate_esby_install(_build_install(tmp_path))
    by_source = {r.source: r for r in state.learning_rules}
    rule = by_source["esby-policy:internal_low_priority_reply"]
    assert "email-composer" in rule.skill_tags
    assert "draft" in rule.correction.lower()
    # reversible_only is true on this rule — should appear in correction text
    assert "reversible" in rule.correction.lower()


def test_correction_text_includes_confidence_threshold(tmp_path: Path) -> None:
    state = migrate_esby_install(_build_install(tmp_path))
    by_source = {r.source: r for r in state.learning_rules}
    rule = by_source["esby-policy:global_no_send_principal_client"]
    assert "0.9" in rule.correction


# ── Settings overlay ──────────────────────────────────────────────────────


def test_preferences_autonomy_bias_drives_settings(tmp_path: Path) -> None:
    """preferences.yaml says option_a → that should map to cautious."""
    state = migrate_esby_install(_build_install(tmp_path), settings_preset="balanced")
    # Esby's preference wins over the CLI arg
    assert state.settings_overlay["autonomy"]["default_policy"] == "cautious"


def test_settings_preset_used_when_no_preference(tmp_path: Path) -> None:
    """If preferences.yaml has no autonomy_bias, the CLI preset wins."""
    root = _build_install(tmp_path)
    # Strip the autonomy_bias from preferences
    (root / "config" / "preferences.yaml").write_text(yaml.safe_dump({"version": 1, "preferences": {}}))
    state = migrate_esby_install(root, settings_preset="aggressive")
    assert state.settings_overlay["autonomy"]["default_policy"] == "aggressive"


# ── Thoughts: configs + setup-report ───────────────────────────────────────


def test_each_config_yaml_lands_as_thought(tmp_path: Path) -> None:
    state = migrate_esby_install(_build_install(tmp_path))
    config_files = {
        s.source_uri
        for s in state.sources
        if s.source_kind == "esby_config"
    }
    assert "config/preferences.yaml" in config_files
    assert "config/autonomy-matrix.yaml" in config_files
    assert "config/stakeholder_classes.yaml" in config_files


def test_setup_report_lands_as_thought(tmp_path: Path) -> None:
    state = migrate_esby_install(_build_install(tmp_path))
    setup_sources = [s for s in state.sources if s.source_kind == "esby_setup"]
    assert len(setup_sources) >= 1


# ── Missing inputs handled gracefully ─────────────────────────────────────


def test_missing_sqlite_recorded_as_skipped(tmp_path: Path) -> None:
    root = tmp_path / "no_db"
    root.mkdir()
    # Section body needs to be at least 30 chars for the chunker to keep it.
    (root / "setup-report.md").write_text(
        "# Setup\n\n## Section\n\nThis is a longer body that exceeds the chunker minimum length."
    )
    state = migrate_esby_install(root)
    assert any("chief_of_staff.sqlite" in s for s in state.skipped_inputs)
    assert state.people == []
    # Other inputs still processed (the setup-report.md got chunked)
    assert state.thoughts


def test_missing_config_dir_recorded_as_skipped(tmp_path: Path) -> None:
    root = tmp_path / "x"
    root.mkdir()
    state = migrate_esby_install(root)
    assert "config/" in state.skipped_inputs


# ── Old vault inclusion ───────────────────────────────────────────────────


def test_old_vault_off_by_default(tmp_path: Path) -> None:
    root = _build_install(tmp_path)
    # Build a sibling .old EsbyVault dir with a markdown file
    old = tmp_path / ".old EsbyVault" / "Esby"
    old.mkdir(parents=True)
    (old / "note.md").write_text("# Title\n## Section\n" + "x" * 80)
    state = migrate_esby_install(root)
    # No old-vault thoughts in the output
    old_count = sum(1 for s in state.sources if s.source_kind == "old_esby_vault")
    assert old_count == 0


def test_old_vault_included_when_flag_set(tmp_path: Path) -> None:
    root = _build_install(tmp_path)
    old = tmp_path / ".old EsbyVault" / "Esby"
    old.mkdir(parents=True)
    (old / "note.md").write_text("# Title\n## Section\n" + "y" * 80)
    state = migrate_esby_install(root, include_old_vault=True)
    old_count = sum(1 for s in state.sources if s.source_kind == "old_esby_vault")
    assert old_count >= 1


# ── Backup-payload shape ──────────────────────────────────────────────────


def test_payload_includes_person_table(tmp_path: Path) -> None:
    payload = esby_to_backup_payload(migrate_esby_install(_build_install(tmp_path)))
    assert "person" in payload["tables"]
    assert len(payload["tables"]["person"]) == 3


def test_payload_includes_learning_rule_table(tmp_path: Path) -> None:
    payload = esby_to_backup_payload(migrate_esby_install(_build_install(tmp_path)))
    assert "learning_rule" in payload["tables"]
    # 2 enabled rules; 1 disabled → 2 in payload
    assert len(payload["tables"]["learning_rule"]) == 2


def test_payload_manifest_records_migration_source(tmp_path: Path) -> None:
    payload = esby_to_backup_payload(migrate_esby_install(_build_install(tmp_path)))
    assert payload["manifest"]["migration_source"] == "esby_install"


# ── End-to-end: migrate → restore → verify ────────────────────────────────


def test_e2e_migrate_then_restore_into_fresh_db(tmp_path: Path) -> None:
    payload = esby_to_backup_payload(migrate_esby_install(_build_install(tmp_path)))
    target_db_path = tmp_path / "target.db"
    target = Database.sqlite(target_db_path)
    target.create_all()
    restore_backup(target, payload, confirm=True, skip_schema_check=True)

    with target.session() as s:
        people = list(s.exec(select(Person)).all())
        rules = list(s.exec(select(LearningRule)).all())
        thoughts = list(s.exec(select(Thought)).all())

    assert len(people) == 3
    assert {p.name for p in people} == {"Robyne", "Charlotte", "Jessica"}
    # never_autonomous_send round-trips correctly
    by_name = {p.name: p for p in people}
    assert by_name["Charlotte"].never_autonomous_send is True
    assert by_name["Robyne"].never_autonomous_send is False
    # autonomy_override round-trips
    assert by_name["Robyne"].autonomy_override == AutonomyOverride.inherit
    # 2 learning rules (1 disabled)
    assert len(rules) == 2
    # configs + setup-report = 4 thoughts
    assert len(thoughts) == 4


def test_esby_install_migration_class_wraps_run(tmp_path: Path) -> None:
    runner = EsbyInstallMigration(install_root=_build_install(tmp_path))
    payload = runner.run()
    assert payload["manifest"]["migration_source"] == "esby_install"
    assert "person" in payload["tables"]


# ── CLI ───────────────────────────────────────────────────────────────────


def test_cli_dry_run(tmp_path: Path) -> None:
    root = _build_install(tmp_path)
    output = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(
        migrate_group,
        ["from-esby-install", str(root), "-o", str(output), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    assert not output.exists()


def test_cli_writes_backup(tmp_path: Path) -> None:
    root = _build_install(tmp_path)
    output = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(
        migrate_group,
        ["from-esby-install", str(root), "-o", str(output)],
    )
    assert result.exit_code == 0, result.output
    assert output.exists()
    payload = json.loads(output.read_text())
    assert payload["manifest"]["migration_source"] == "esby_install"


def test_cli_include_old_vault_flag(tmp_path: Path) -> None:
    root = _build_install(tmp_path)
    old = tmp_path / ".old EsbyVault" / "Esby"
    old.mkdir(parents=True)
    (old / "note.md").write_text("# Title\n## Section\n" + "z" * 80)
    output = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(
        migrate_group,
        [
            "from-esby-install",
            str(root),
            "-o",
            str(output),
            "--include-old-vault",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text())
    sources = payload["tables"]["thought_source"]
    assert any(s["source_kind"] == "old_esby_vault" for s in sources)
