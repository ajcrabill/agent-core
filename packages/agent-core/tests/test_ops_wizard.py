"""Tests for agent_core.ops.wizard — 3-tier interview-style setup."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_core.ops.wizard import (
    SetupWizard,
    WizardValidationError,
    dict_io,
)
from agent_core.settings import AgentSettings


# ── Tier 1 ─────────────────────────────────────────────────────────────────


def test_tier1_minimum_viable_install() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "Test User",
        "Storage backend (sqlite|postgres)": "sqlite",
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=1)
    assert result.tier == 1
    assert result.settings.autonomy.default_policy == "balanced"
    assert result.overrides["__display_name"] == "Test User"


def test_tier1_cautious_preset_applied() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "cautious",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=1)
    # Cautious preset should disable agentic feedback and use loose detector
    assert result.settings.learning.agentic_feedback_enabled is False
    assert result.settings.learning.detector_strictness == "loose"


def test_tier1_aggressive_preset_applied() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "aggressive",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=1)
    assert result.settings.learning.synthetic_battery_enabled is True
    assert result.settings.notifications.urgency_floor == "info"


def test_tier1_rejects_unknown_preset() -> None:
    answers = {"Choose a preset (cautious|balanced|aggressive)": "yolo"}
    with pytest.raises(WizardValidationError):
        SetupWizard(io=dict_io(answers)).run(tier=1)


def test_tier1_rejects_unknown_backend() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "mongodb",
    }
    with pytest.raises(WizardValidationError):
        SetupWizard(io=dict_io(answers)).run(tier=1)


def test_tier1_postgres_backend_recorded() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "postgres",
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=1)
    assert result.settings.storage.backend == "postgres"
    assert result.overrides["storage.backend"] == "postgres"


# ── Tier 2 ─────────────────────────────────────────────────────────────────


def test_tier2_enables_notifications_with_topic() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
        "Enable push notifications via ntfy?": True,
        "ntfy topic (pick something unguessable, e.g. 'dcos-7x9k')": "private-topic-x9",
        "Urgency floor (info|warn|critical — only this and above push)": "warn",
        "Vault path (Obsidian-style; leave blank to skip)": "",
        "Embedding provider (ollama|stub|stub-semantic)": "ollama",
        "Enable mesh peering with other agents?": False,
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=2)
    assert result.settings.notifications.enabled is True
    assert result.settings.notifications.ntfy_topic == "private-topic-x9"
    assert result.settings.notifications.urgency_floor == "warn"


def test_tier2_notifications_off_skips_topic_question() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "cautious",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
        "Enable push notifications via ntfy?": False,
        "Vault path (Obsidian-style; leave blank to skip)": "",
        "Embedding provider (ollama|stub|stub-semantic)": "ollama",
        "Enable mesh peering with other agents?": False,
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=2)
    assert result.settings.notifications.enabled is False
    # The cautious preset already had it off; no override added.
    assert "notifications.ntfy_topic" not in result.overrides


def test_tier2_notifications_enabled_without_topic_raises() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
        "Enable push notifications via ntfy?": True,
        "ntfy topic (pick something unguessable, e.g. 'dcos-7x9k')": "",
        "Urgency floor (info|warn|critical — only this and above push)": "critical",
    }
    with pytest.raises(WizardValidationError, match="topic"):
        SetupWizard(io=dict_io(answers)).run(tier=2)


def test_tier2_vault_path_recorded(tmp_path: Path) -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
        "Enable push notifications via ntfy?": False,
        "Vault path (Obsidian-style; leave blank to skip)": str(tmp_path),
        "Embedding provider (ollama|stub|stub-semantic)": "ollama",
        "Enable mesh peering with other agents?": False,
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=2)
    assert result.settings.storage.vault_path == str(tmp_path)


def test_tier2_embedding_provider_change() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
        "Enable push notifications via ntfy?": False,
        "Vault path (Obsidian-style; leave blank to skip)": "",
        "Embedding provider (ollama|stub|stub-semantic)": "stub",
        "Enable mesh peering with other agents?": False,
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=2)
    assert result.settings.openbrain.embedding_provider == "stub"
    assert result.overrides["openbrain.embedding_provider"] == "stub"


def test_tier2_rejects_unknown_embedding_provider() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
        "Enable push notifications via ntfy?": False,
        "Vault path (Obsidian-style; leave blank to skip)": "",
        "Embedding provider (ollama|stub|stub-semantic)": "openai",
        "Enable mesh peering with other agents?": False,
    }
    with pytest.raises(WizardValidationError):
        SetupWizard(io=dict_io(answers)).run(tier=2)


def test_tier2_mesh_enable() -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
        "Enable push notifications via ntfy?": False,
        "Vault path (Obsidian-style; leave blank to skip)": "",
        "Embedding provider (ollama|stub|stub-semantic)": "ollama",
        "Enable mesh peering with other agents?": True,
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=2)
    assert result.settings.mesh.enabled is True


# ── Tier 3 (smoke) ─────────────────────────────────────────────────────────


def test_tier3_walks_all_fields_without_changes() -> None:
    """If the user just hits return through tier 3, the result equals the
    tier-2 result (all defaults preserved)."""
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
        "Enable push notifications via ntfy?": False,
        "Vault path (Obsidian-style; leave blank to skip)": "",
        "Embedding provider (ollama|stub|stub-semantic)": "ollama",
        "Enable mesh peering with other agents?": False,
    }
    # Tier 3 falls through to ask every leaf — dict_io returns "" by default
    # so values stay as their tier-2 settings.
    result = SetupWizard(io=dict_io(answers)).run(tier=3)
    # Cross-check with tier 2 baseline
    baseline = SetupWizard(io=dict_io(answers)).run(tier=2)
    assert result.settings.model_dump() == baseline.settings.model_dump()


# ── commit() ───────────────────────────────────────────────────────────────


def test_commit_writes_minimal_diff(tmp_path: Path) -> None:
    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "cautious",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=1)
    out = tmp_path / "agent.yml"
    result.commit(out)
    on_disk = yaml.safe_load(out.read_text())
    # Cautious preset changes a bunch of things — file should reflect them
    assert on_disk["autonomy"]["default_policy"] == "cautious"
    # And NOT contain values that match defaults (e.g., storage section
    # has no overrides since sqlite is the default)
    assert "storage" not in on_disk


def test_commit_results_in_loadable_settings(tmp_path: Path) -> None:
    """End-to-end: wizard → commit → SettingsManager reads back cleanly."""
    from agent_core.settings import SettingsManager

    answers = {
        "Choose a preset (cautious|balanced|aggressive)": "balanced",
        "Your display name": "",
        "Storage backend (sqlite|postgres)": "sqlite",
        "Enable push notifications via ntfy?": True,
        "ntfy topic (pick something unguessable, e.g. 'dcos-7x9k')": "wizard-test-7x9",
        "Urgency floor (info|warn|critical — only this and above push)": "critical",
        "Vault path (Obsidian-style; leave blank to skip)": "",
        "Embedding provider (ollama|stub|stub-semantic)": "ollama",
        "Enable mesh peering with other agents?": False,
    }
    result = SetupWizard(io=dict_io(answers)).run(tier=2)
    out = tmp_path / "agent.yml"
    result.commit(out)

    mgr = SettingsManager(path=out, env={})
    assert mgr.get("notifications.enabled") is True
    assert mgr.get("notifications.ntfy_topic") == "wizard-test-7x9"


def test_invalid_tier_raises() -> None:
    with pytest.raises(ValueError, match="tier must be 1, 2, or 3"):
        SetupWizard(io=dict_io({})).run(tier=4)  # type: ignore[arg-type]
