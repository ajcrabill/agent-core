"""Tests for agent_core.migrations.from_loriah_vault.

Two flavors:
  - Unit tests against synthetic vault structure (in tmp_path).
  - End-to-end roundtrip: synthesize a vault → migrate → restore_backup
    into a fresh DB → verify the obligations + thoughts landed."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from agent_core.migrations import (
    LoriahVaultMigration,
    migrate_loriah_vault,
    to_backup_payload,
)
from agent_core.migrations.cli import migrate_group
from agent_core.migrations.from_loriah_vault import (
    DEFAULT_VAULT_PATHS,
    SEED_OBLIGATIONS,
)
from agent_core.ops import restore_backup
from agent_core.state import Database
from agent_core.state.models import Obligation, Thought, ThoughtSource
from sqlmodel import select


# ── Vault fixture ──────────────────────────────────────────────────────────


def _build_vault(tmp_path: Path, *, with_all_files: bool = True) -> Path:
    """Build a synthetic vault with the three expected markdown files."""
    vault = tmp_path / "vault"
    for label, rel in DEFAULT_VAULT_PATHS.items():
        if not with_all_files and label == "learning_log":
            continue
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_FIXTURES[label])
    return vault


_FIXTURES = {
    "operational_state": """---
type: admin
date-created: 2026-04-30
---

# Operational State

**Last Updated**: 2026-05-02

## Sprint Progress

Sprint 0 complete. Repo at https://github.com/ajcrabill/agent-core. Cumulative
236 tests passing. Stack covers state layer, context loader, agent loop,
work layer (inbound + pipeline + incidents), quality auditor, action policy.

## Active Project: dCoS / iKB Packaging

AJ packaging Loriah → dcos-agent and Esby → ikb-agent on shared agent-core.
Plan: 10 weeks focused, 12-14 calendar weeks realistic.

## Awaiting AJ

- Hermes deepseek-chat fix
- API key rotation
""",
    "conversation_journal": """---
type: admin
---

# Conversation Journal

**Last Updated**: 2026-04-30

## Active Threads

### 1. CMS Board Meeting Evaluation
**Status**: Active revision cycle. Esby has sent multiple versions (v3, v5,
v6) after file corruption issues. AJ provided detailed feedback on v3:
missing agenda items (4a-4d, 5a-5c, 5f), duplicate item (5d).
**AJ's decision point**: Approve final version or request additional changes?

### 2. Charlotte Grinberg Social Coordination
**Status**: New, pending response. Reached out via Google Voice SMS. Asked
"How old are your kids?" Proposed May 11 dinner; family free 5:30–7:30 PM.

## Concluded Threads

### Dialog 2026 Table 37 Dinner
High-engagement social dinner with peers (US President roleplay, Nobel
Prize, UN Secretary-General, etc.). Multiple thank-you messages from
attendees. Status: Concluded.
""",
    "learning_log": """---
type: admin
---

# Learning Log Data

**Status**: LEARNING PHASE
**Last Batch Classification**: 2026-04-30 07:12 CDT

## aj-inbox-classifier Rules

### v1.0 — Bootstrap Rules

Default triage taxonomy:

- action:flag — Requires AJ decision or response
- action:archive — Newsletters, confirmations, FYI
- action:hold — Important but not urgent

## Classification Audit Trail

### Batch 1 — 2026-04-30

24 emails classified. Distribution: 18 archive, 4 flag, 1 task, 1 track.
""",
}


# ── Core migration ─────────────────────────────────────────────────────────


def test_migration_extracts_thoughts_for_each_section(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    state = migrate_loriah_vault(vault)
    # Each fixture has 3 substantive sections; total ~9 thoughts (sections
    # under 30 chars are dropped — gives some slop).
    assert len(state.thoughts) >= 6
    assert len(state.sources) == len(state.thoughts)


def test_migration_attaches_source_provenance(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    state = migrate_loriah_vault(vault)
    # Every source row points back to the relative vault path
    paths = {s.source_uri for s in state.sources}
    assert any("operational-state.md" in p for p in paths)
    assert any("conversation-journal.md" in p for p in paths)
    assert any("learning-log-data.md" in p for p in paths)


def test_migration_titles_use_section_headings(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    state = migrate_loriah_vault(vault)
    titles = [s.source_title for s in state.sources]
    assert any("Sprint Progress" in (t or "") for t in titles)
    assert any("Active Threads" in (t or "") or "CMS Board" in (t or "") for t in titles)


def test_migration_includes_seed_obligations_by_default(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    state = migrate_loriah_vault(vault)
    assert len(state.obligations) == len(SEED_OBLIGATIONS)
    titles = {o.title for o in state.obligations}
    assert any("CMS Board" in t for t in titles)
    assert any("Charlotte" in t for t in titles)
    assert any("Drive share" in t for t in titles)


def test_migration_can_skip_seed_obligations(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    state = migrate_loriah_vault(vault, include_seed_obligations=False)
    assert state.obligations == []
    # Thoughts still imported
    assert state.thoughts


def test_migration_handles_missing_files_gracefully(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, with_all_files=False)
    state = migrate_loriah_vault(vault)
    assert "learning-log" in (str(state.skipped_files))
    # Other thoughts still imported
    assert state.thoughts


def test_migration_skips_short_sections(tmp_path: Path) -> None:
    """Sections under 30 chars are dropped (navigational headers, not content)."""
    vault = tmp_path / "vault"
    rel = DEFAULT_VAULT_PATHS["operational_state"]
    f = vault / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Title\n\n## A\n\nshort\n\n## B\n\n" + "x" * 50)
    # Other expected files missing — that's fine, they get skipped
    state = migrate_loriah_vault(vault)
    contents = [t.content.strip() for t in state.thoughts]
    assert all("short" not in c for c in contents)
    assert any("xxxxx" in c for c in contents)


def test_migration_strips_yaml_frontmatter(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    rel = DEFAULT_VAULT_PATHS["operational_state"]
    f = vault / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "---\nfrontmatter: yes\n---\n\n## Real Content\n\nThis is the actual body, lots of real text here."
    )
    state = migrate_loriah_vault(vault)
    contents = " ".join(t.content for t in state.thoughts)
    assert "frontmatter: yes" not in contents
    assert "actual body" in contents


def test_settings_overlay_includes_preset_and_vault_path(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    state = migrate_loriah_vault(vault, settings_preset="cautious")
    assert state.settings_overlay["autonomy"]["default_policy"] == "cautious"
    assert state.settings_overlay["storage"]["vault_path"] == str(vault.resolve())


def test_obligations_are_principal_or_agent_owned_with_principal_ratification(
    tmp_path: Path,
) -> None:
    """Seed obligations should require explicit principal ratification —
    they're guesses about what the user might want."""
    state = migrate_loriah_vault(_build_vault(tmp_path))
    for o in state.obligations:
        assert any(c.get("type") == "principal_ratification" for c in o.completion_criteria)


# ── Backup payload shape ──────────────────────────────────────────────────


def test_to_backup_payload_has_correct_shape(tmp_path: Path) -> None:
    state = migrate_loriah_vault(_build_vault(tmp_path))
    payload = to_backup_payload(state)
    assert "manifest" in payload
    assert "tables" in payload
    assert payload["manifest"]["format_version"] == 1
    assert payload["manifest"]["migration_source"] == "loriah_vault"
    assert "obligation" in payload["tables"]
    assert "thought" in payload["tables"]
    assert "thought_source" in payload["tables"]


def test_to_backup_payload_embeds_settings_yaml(tmp_path: Path) -> None:
    state = migrate_loriah_vault(_build_vault(tmp_path), settings_preset="aggressive")
    payload = to_backup_payload(state)
    assert "settings_yaml" in payload
    parsed = yaml.safe_load(payload["settings_yaml"])
    assert parsed["autonomy"]["default_policy"] == "aggressive"


def test_obligation_dicts_are_restore_compatible(tmp_path: Path) -> None:
    """Each obligation dict has the keys restore_backup expects."""
    state = migrate_loriah_vault(_build_vault(tmp_path))
    payload = to_backup_payload(state)
    sample = payload["tables"]["obligation"][0]
    for key in ("id", "title", "status", "owner", "source", "created_at", "updated_at"):
        assert key in sample, f"missing {key} from obligation row"


def test_thought_dicts_have_null_embedding(tmp_path: Path) -> None:
    """Thoughts come in unindexed; the user runs reindex after restore to
    have them searchable."""
    state = migrate_loriah_vault(_build_vault(tmp_path))
    payload = to_backup_payload(state)
    for t in payload["tables"]["thought"]:
        assert t["embedding"] is None
        assert t["embedding_model"] is None


# ── End-to-end: migrate → restore → verify ────────────────────────────────


def test_e2e_migrate_then_restore(tmp_path: Path) -> None:
    """Full happy path: migrate vault → restore_backup → query the new db."""
    vault = _build_vault(tmp_path)
    payload = to_backup_payload(migrate_loriah_vault(vault))

    target_db_path = tmp_path / "target.db"
    target = Database.sqlite(target_db_path)
    target.create_all()
    restore_backup(target, payload, confirm=True, skip_schema_check=True)

    with target.session() as s:
        ob_count = len(list(s.exec(select(Obligation)).all()))
        thought_count = len(list(s.exec(select(Thought)).all()))
        source_count = len(list(s.exec(select(ThoughtSource)).all()))

    assert ob_count == len(SEED_OBLIGATIONS)
    assert thought_count >= 6
    assert source_count == thought_count


def test_e2e_settings_yaml_writes_when_path_passed(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    payload = to_backup_payload(
        migrate_loriah_vault(vault, settings_preset="cautious")
    )

    target_db = Database.sqlite(tmp_path / "target.db")
    target_db.create_all()
    target_yml = tmp_path / "agent.yml"

    restore_backup(
        target_db,
        payload,
        confirm=True,
        skip_schema_check=True,
        settings_path=target_yml,
    )
    assert target_yml.exists()
    parsed = yaml.safe_load(target_yml.read_text())
    assert parsed["autonomy"]["default_policy"] == "cautious"


def test_loriah_vault_migration_class_wraps_run() -> None:
    """The convenience class returns the same payload shape."""
    runner = LoriahVaultMigration(
        vault_path=Path(__file__).parent / "fixtures",  # any path; will skip files
    )
    payload = runner.run()
    # No vault files at this path → no thoughts, but seeds + settings still emit
    assert payload["manifest"]["format_version"] == 1
    assert "settings_yaml" in payload


# ── CLI ───────────────────────────────────────────────────────────────────


def test_cli_dry_run_doesnt_write(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    output = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(
        migrate_group,
        ["from-loriah-vault", str(vault), "-o", str(output), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    assert not output.exists()


def test_cli_writes_backup_file(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    output = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(
        migrate_group,
        ["from-loriah-vault", str(vault), "-o", str(output)],
    )
    assert result.exit_code == 0, result.output
    assert output.exists()
    assert output.stat().st_size > 100


def test_cli_reports_skipped_files(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, with_all_files=False)
    output = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(
        migrate_group,
        ["from-loriah-vault", str(vault), "-o", str(output), "--dry-run"],
    )
    assert result.exit_code == 0
    assert "missing" in result.output.lower() or "not found" in result.output.lower()


def test_cli_no_seed_obligations(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path)
    output = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(
        migrate_group,
        [
            "from-loriah-vault",
            str(vault),
            "-o",
            str(output),
            "--no-seed-obligations",
        ],
    )
    assert result.exit_code == 0, result.output
    import json as _json

    payload = _json.loads(output.read_text())
    assert payload["tables"]["obligation"] == []
