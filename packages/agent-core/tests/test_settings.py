"""Tests for agent_core.settings — schema, manager (resolution + persistence), presets, CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from agent_core.settings import (
    PRESETS,
    AgentSettings,
    SettingsManager,
    SettingsSource,
    apply_preset,
    list_presets,
)
from agent_core.settings.cli import settings_group
from agent_core.settings.manager import SettingsError
from click.testing import CliRunner

# ── Schema basics ──────────────────────────────────────────────────────────


def test_defaults_construct_cleanly() -> None:
    s = AgentSettings()
    assert s.autonomy.default_policy == "balanced"
    assert s.notifications.enabled is False
    assert s.notifications.urgency_floor == "critical"
    assert s.openbrain.embedding_provider == "ollama"
    assert s.work.pipeline_in_progress_threshold_hours == 24.0


def test_invalid_choice_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentSettings(autonomy={"default_policy": "yolo"})  # type: ignore[arg-type]


def test_extra_field_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentSettings(autonomy={"default_policy": "balanced", "totally_made_up": True})  # type: ignore[arg-type]


def test_archive_settings_present_and_default_safe() -> None:
    s = AgentSettings()
    assert s.autonomy.archive_instead_of_delete is True
    assert s.autonomy.archive_retention_days == 30
    assert s.autonomy.require_confirm_for_hard_delete is True


# ── Manager: defaults-only ─────────────────────────────────────────────────


def test_manager_loads_defaults_when_no_file(tmp_path: Path) -> None:
    mgr = SettingsManager(path=tmp_path / "agent.yml", env={})
    assert mgr.settings.autonomy.default_policy == "balanced"
    assert mgr.get("autonomy.default_policy") == "balanced"
    assert mgr.get_with_source("autonomy.default_policy").source == SettingsSource.default


# ── Manager: file overlay ──────────────────────────────────────────────────


def test_manager_reads_yaml_overrides(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    p.write_text(yaml.safe_dump({"autonomy": {"default_policy": "cautious"}}))
    mgr = SettingsManager(path=p, env={})
    assert mgr.get("autonomy.default_policy") == "cautious"
    assert mgr.get_with_source("autonomy.default_policy").source == SettingsSource.file


def test_yaml_invalid_value_raises(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    p.write_text(yaml.safe_dump({"autonomy": {"default_policy": "nonsense"}}))
    with pytest.raises(SettingsError):
        SettingsManager(path=p, env={})


def test_yaml_top_level_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    p.write_text("- one\n- two\n")
    with pytest.raises(SettingsError):
        SettingsManager(path=p, env={})


# ── Manager: env-var overlay ───────────────────────────────────────────────


def test_env_overrides_yaml(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    p.write_text(yaml.safe_dump({"autonomy": {"default_policy": "cautious"}}))
    env = {"AGENT_AUTONOMY__DEFAULT_POLICY": "aggressive"}
    mgr = SettingsManager(path=p, env=env)
    assert mgr.get("autonomy.default_policy") == "aggressive"
    assert mgr.get_with_source("autonomy.default_policy").source == SettingsSource.env


def test_env_coercion(tmp_path: Path) -> None:
    env = {
        "AGENT_NOTIFICATIONS__ENABLED": "true",
        "AGENT_NOTIFICATIONS__NTFY_TOPIC": "private-topic-xyz",
        "AGENT_OPENBRAIN__SEARCH_DEFAULT_LIMIT": "20",
        "AGENT_OPENBRAIN__SEARCH_DEFAULT_THRESHOLD": "0.4",
    }
    mgr = SettingsManager(path=tmp_path / "agent.yml", env=env)
    assert mgr.get("notifications.enabled") is True
    assert mgr.get("notifications.ntfy_topic") == "private-topic-xyz"
    assert mgr.get("openbrain.search_default_limit") == 20
    assert mgr.get("openbrain.search_default_threshold") == pytest.approx(0.4)


def test_env_without_section_delimiter_ignored(tmp_path: Path) -> None:
    env = {"AGENT_FOO": "bar"}
    mgr = SettingsManager(path=tmp_path / "agent.yml", env=env)
    # Loads cleanly, doesn't crash, doesn't pollute
    assert mgr.get("autonomy.default_policy") == "balanced"


# ── Manager: set / reset / persistence ─────────────────────────────────────


def test_set_persists_atomically(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    mgr = SettingsManager(path=p, env={})
    mgr.set("learning.detector_strictness", "strict")
    assert mgr.get("learning.detector_strictness") == "strict"
    on_disk = yaml.safe_load(p.read_text())
    assert on_disk == {"learning": {"detector_strictness": "strict"}}


def test_set_rejects_invalid_value(tmp_path: Path) -> None:
    mgr = SettingsManager(path=tmp_path / "agent.yml", env={})
    with pytest.raises(SettingsError):
        mgr.set("autonomy.default_policy", "yolo")


def test_reset_one_key(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    mgr = SettingsManager(path=p, env={})
    mgr.set("learning.detector_strictness", "strict")
    mgr.set("autonomy.default_policy", "cautious")
    mgr.reset("learning.detector_strictness")
    assert mgr.get("learning.detector_strictness") == "balanced"  # back to default
    assert mgr.get("autonomy.default_policy") == "cautious"  # untouched


def test_reset_all_clears_file(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    mgr = SettingsManager(path=p, env={})
    mgr.set("learning.detector_strictness", "strict")
    mgr.reset()
    assert mgr.get("learning.detector_strictness") == "balanced"
    assert yaml.safe_load(p.read_text()) == {}


def test_unknown_path_raises(tmp_path: Path) -> None:
    mgr = SettingsManager(path=tmp_path / "agent.yml", env={})
    with pytest.raises(KeyError):
        mgr.get("nonsense.key")


# ── Manager: source tracking ───────────────────────────────────────────────


def test_all_with_sources_covers_every_leaf(tmp_path: Path) -> None:
    mgr = SettingsManager(path=tmp_path / "agent.yml", env={})
    rows = mgr.all_with_sources()
    paths = {r.path for r in rows}
    # Spot-check sections all surfaced
    assert "autonomy.default_policy" in paths
    assert "learning.detector_strictness" in paths
    assert "notifications.enabled" in paths
    assert "openbrain.embedding_provider" in paths
    assert "work.pipeline_in_progress_threshold_hours" in paths
    assert "runtime.max_obligations_per_tick" in paths
    # All start as defaults
    assert all(r.source == SettingsSource.default for r in rows)


# ── Presets ────────────────────────────────────────────────────────────────


def test_three_presets_exist() -> None:
    names = list_presets()
    assert set(names) == {"cautious", "balanced", "aggressive"}


def test_each_preset_validates() -> None:
    for name in PRESETS:
        result = apply_preset(AgentSettings(), name)  # type: ignore[arg-type]
        assert isinstance(result, AgentSettings)


def test_cautious_preset_disables_notifications_and_softens() -> None:
    result = apply_preset(AgentSettings(), "cautious")
    assert result.notifications.enabled is False
    assert result.learning.detector_strictness == "loose"
    assert result.learning.agentic_feedback_enabled is False
    assert result.autonomy.archive_retention_days == 90


def test_aggressive_preset_enables_synthetic_battery_and_strict() -> None:
    result = apply_preset(AgentSettings(), "aggressive")
    assert result.learning.synthetic_battery_enabled is True
    assert result.learning.detector_strictness == "strict"
    assert result.notifications.urgency_floor == "info"
    assert result.autonomy.archive_instead_of_delete is False


def test_unknown_preset_raises() -> None:
    with pytest.raises(KeyError):
        apply_preset(AgentSettings(), "unknown")  # type: ignore[arg-type]


def test_apply_preset_through_manager_writes_minimal_diff(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    mgr = SettingsManager(path=p, env={})
    mgr.apply_preset("cautious")
    on_disk = yaml.safe_load(p.read_text()) or {}
    # Should NOT contain values that already match defaults — only the diff
    flat = _flatten(on_disk)
    # 'storage' section has no preset overrides → not in the file
    assert not any(k.startswith("storage.") for k in flat)
    # 'autonomy.default_policy' differs from default ('balanced' → 'cautious')
    assert flat.get("autonomy.default_policy") == "cautious"


# ── CLI smoke ──────────────────────────────────────────────────────────────


def test_cli_show_table(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    runner = CliRunner()
    result = runner.invoke(settings_group, ["--config", str(p), "show"])
    assert result.exit_code == 0
    assert "autonomy.default_policy" in result.output


def test_cli_show_section(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    runner = CliRunner()
    result = runner.invoke(settings_group, ["--config", str(p), "show", "learning"])
    assert result.exit_code == 0
    assert "learning.detector_strictness" in result.output
    assert "autonomy.default_policy" not in result.output


def test_cli_show_json(tmp_path: Path) -> None:
    import json

    runner = CliRunner()
    result = runner.invoke(
        settings_group,
        ["--config", str(tmp_path / "agent.yml"), "show", "notifications", "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert all("path" in r and "value" in r and "source" in r for r in payload)


def test_cli_set_persists(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    runner = CliRunner()
    result = runner.invoke(
        settings_group,
        ["--config", str(p), "set", "autonomy.default_policy=cautious"],
    )
    assert result.exit_code == 0, result.output
    on_disk = yaml.safe_load(p.read_text())
    assert on_disk["autonomy"]["default_policy"] == "cautious"


def test_cli_set_rejects_invalid(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        settings_group,
        ["--config", str(tmp_path / "agent.yml"), "set", "autonomy.default_policy=yolo"],
    )
    assert result.exit_code != 0
    assert "rejected" in result.output.lower()


def test_cli_reset_one(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    runner = CliRunner()
    runner.invoke(
        settings_group, ["--config", str(p), "set", "learning.detector_strictness=strict"]
    )
    result = runner.invoke(
        settings_group, ["--config", str(p), "reset", "learning.detector_strictness"]
    )
    assert result.exit_code == 0
    on_disk = yaml.safe_load(p.read_text()) or {}
    assert "learning" not in on_disk or "detector_strictness" not in on_disk.get("learning", {})


def test_cli_preset_list() -> None:
    runner = CliRunner()
    result = runner.invoke(settings_group, ["preset", "list"])
    assert result.exit_code == 0
    assert "cautious" in result.output
    assert "balanced" in result.output
    assert "aggressive" in result.output


def test_cli_preset_show_diff(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        settings_group, ["--config", str(tmp_path / "agent.yml"), "preset", "show", "cautious"]
    )
    assert result.exit_code == 0
    # Diff should mention learning.detector_strictness (balanced → loose)
    assert "detector_strictness" in result.output


def test_cli_preset_apply(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    runner = CliRunner()
    result = runner.invoke(
        settings_group,
        ["--config", str(p), "preset", "apply", "cautious", "--yes"],
    )
    assert result.exit_code == 0, result.output
    on_disk = yaml.safe_load(p.read_text())
    assert on_disk["autonomy"]["default_policy"] == "cautious"


def test_cli_path_reports_location(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    runner = CliRunner()
    result = runner.invoke(settings_group, ["--config", str(p), "path"])
    assert result.exit_code == 0
    # Rich may wrap long paths; check the filename and a path fragment instead.
    assert "agent.yml" in result.output
    assert tmp_path.name in result.output


def test_cli_doctor_clean(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(settings_group, ["--config", str(tmp_path / "agent.yml"), "doctor"])
    assert result.exit_code == 0
    assert "ok" in result.output.lower()


def test_cli_doctor_lists_overrides(tmp_path: Path) -> None:
    p = tmp_path / "agent.yml"
    runner = CliRunner()
    runner.invoke(
        settings_group, ["--config", str(p), "set", "learning.detector_strictness=strict"]
    )
    result = runner.invoke(settings_group, ["--config", str(p), "doctor"])
    assert result.exit_code == 0
    assert "learning.detector_strictness" in result.output


# ── Helpers ────────────────────────────────────────────────────────────────


def _flatten(d: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, path))
        else:
            out[path] = v
    return out
