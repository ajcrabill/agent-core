"""Named presets — opinionated bundles of settings overrides.

A preset is a sparse partial of ``AgentSettings``. Applying a preset:

    SettingsManager.apply_preset("cautious")

deep-merges the preset's overlay onto the current settings, then writes
``agent.yml``. Users can still tweak individual fields afterwards.

Three presets ship by default:

  cautious    — for first-time users + sensitive contexts
                * everything gated, including sends/posts
                * notifications off, daily digest only
                * supervised learning at "loose" (only obvious corrections)
                * agentic-feedback off (no auto-promotion of skills)
                * archive-instead-of-delete on, 90-day retention

  balanced (default) — sensible production defaults
                * read/internal/ob writes autonomous; external sends gated
                * critical-only push notifications
                * supervised learning balanced; agentic feedback on
                * archive-instead-of-delete on, 30-day retention

  aggressive  — for power users who want maximum agency
                * most actions autonomous including external sends
                * all push notifications enabled
                * supervised learning strict; synthetic battery on
                * permanent deletes allowed but still confirmed

Add a new preset by registering it in ``PRESETS`` below. Keep them sparse —
only deviations from the schema defaults belong here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent_core.settings.schema import AgentSettings, PresetName

# Preset overlays. Sparse — only the fields that deviate from schema defaults.
PRESETS: dict[PresetName, dict[str, Any]] = {
    "cautious": {
        "autonomy": {
            "default_policy": "cautious",
            "auto_promote_after_n_successes": 25,
            "auto_undelegate_after_n_failures": 1,
            "archive_instead_of_delete": True,
            "archive_retention_days": 90,
            "require_confirm_for_hard_delete": True,
        },
        "learning": {
            "supervised_capture_enabled": True,
            "detector_strictness": "loose",
            "auto_promote_confidence": 0.95,
            "min_observations_to_promote": 5,
            "agentic_feedback_enabled": False,
            "synthetic_battery_enabled": False,
        },
        "notifications": {
            "enabled": False,
            "urgency_floor": "critical",
            "daily_digest_enabled": True,
        },
        "quality": {
            "auditor_enabled": True,
            "audit_sample_rate": 1.0,
        },
    },
    "balanced": {
        "autonomy": {
            "default_policy": "balanced",
            "auto_promote_after_n_successes": 10,
            "auto_undelegate_after_n_failures": 2,
            "archive_instead_of_delete": True,
            "archive_retention_days": 30,
            "require_confirm_for_hard_delete": True,
        },
        "learning": {
            "supervised_capture_enabled": True,
            "detector_strictness": "balanced",
            "auto_promote_confidence": 0.85,
            "min_observations_to_promote": 3,
            "agentic_feedback_enabled": True,
            "synthetic_battery_enabled": False,
        },
        "notifications": {
            # Opt-in. Tier 1 wizard doesn't collect a topic so enabling
            # by default would fail doctor on every fresh install. Users
            # opt in via:
            #   agent settings set notifications.enabled=true \
            #                       notifications.ntfy_topic=<your-topic>
            "enabled": False,
            "urgency_floor": "critical",
            "daily_digest_enabled": True,
        },
        "quality": {
            "auditor_enabled": True,
            "audit_sample_rate": 1.0,
        },
    },
    "aggressive": {
        "autonomy": {
            "default_policy": "aggressive",
            "auto_promote_after_n_successes": 5,
            "auto_undelegate_after_n_failures": 3,
            "archive_instead_of_delete": False,
            "archive_retention_days": 7,
            "require_confirm_for_hard_delete": True,
        },
        "learning": {
            "supervised_capture_enabled": True,
            "detector_strictness": "strict",
            "auto_promote_confidence": 0.75,
            "min_observations_to_promote": 2,
            "agentic_feedback_enabled": True,
            "synthetic_battery_enabled": True,
        },
        "notifications": {
            "enabled": True,
            "urgency_floor": "info",
            "daily_digest_enabled": True,
        },
        "quality": {
            "auditor_enabled": True,
            "audit_sample_rate": 1.0,
        },
    },
}


def list_presets() -> list[PresetName]:
    """Return the names of all built-in presets."""
    return list(PRESETS.keys())


def apply_preset(current: AgentSettings, name: PresetName) -> AgentSettings:
    """Return a new ``AgentSettings`` with ``name``'s overlay applied to ``current``.

    The overlay is deep-merged: nested keys not present in the preset are
    preserved from ``current``. The schema validates the result, so an
    invalid preset (or invalid current state) will raise.

    Raises:
        KeyError: if ``name`` is not a known preset.
        pydantic.ValidationError: if the merged result fails validation.
    """
    if name not in PRESETS:
        raise KeyError(f"unknown preset {name!r}; known: {list(PRESETS)}")
    merged = _deep_merge(current.model_dump(), deepcopy(PRESETS[name]))
    return AgentSettings.model_validate(merged)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge — overlay wins; nested dicts merge instead of replace."""
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


__all__ = ["PRESETS", "apply_preset", "list_presets"]
