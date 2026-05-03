"""Migrate Esby's installed-chief-of-staff state into a fresh ikb-agent install.

Esby's source layout (lifted as ``installed-chief-of-staff/`` on the source
machine):

  state/chief_of_staff.sqlite    — operational data (people, policy_rules, ...)
  config/*.yaml                  — autonomy-matrix, preferences, stakeholder_classes,
                                   policies, vault_map, workflows
  setup-report.md                — install record

What this migration extracts (per Sprint 13 discovery — most tables in the
source DB are empty):

  - **Person rows** from ``people`` (15 in the live source). Maps directly
    to agent_core's new Person table from sprint 13a.

  - **LearningRule rows** from ``policy_rules`` (7 in the live source).
    Each Esby policy gets translated into a learning rule whose
    ``correction`` describes the rule in natural language and whose
    ``skill_tags`` point at the relevant skills (email-composer for
    send_email rules, "general" for system-level rules).

  - **Thoughts** for the YAML configs that don't map cleanly to settings
    (stakeholder_classes, policies-as-data, workflows, vault_map). Stored
    with source_kind='esby_config' so the user can recall design context
    via openbrain search.

  - **Settings overlay** from autonomy-matrix.yaml + preferences.yaml
    where the mapping is clean (autonomy.default_policy + select prefs).

  - **Optional**: chunk markdown from ~/.old EsbyVault/Esby into Thoughts.
    Off by default (``include_old_vault=False``) — turn on to include
    archived vault context.

Tables NOT migrated:
  - workflow_runs (historical execution data, no agent-core equivalent)
  - obligations / threads / events / draft_actions / approvals /
    daily_digest_items / memory_candidates / evaluation_records — all
    empty in the live source as of Sprint 13 discovery.

Migration safety:
  - Read-only against the source SQLite + YAML + markdown.
  - Idempotent at the JSON level (modulo created_at timestamps).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_core.migrations._helpers import (
    _LearningRuleRow,
    _PersonRow,
    _SourceRow,
    _ThoughtRow,
    build_backup_payload,
    chunk_markdown_to_thoughts,
    new_id,
)
from agent_core.state.models import AutonomyOverride

logger = logging.getLogger(__name__)


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass
class MigratedState:
    """Everything the Esby migration extracted, ready for backup-payload."""

    people: list[_PersonRow] = field(default_factory=list)
    learning_rules: list[_LearningRuleRow] = field(default_factory=list)
    thoughts: list[_ThoughtRow] = field(default_factory=list)
    sources: list[_SourceRow] = field(default_factory=list)
    settings_overlay: dict[str, Any] = field(default_factory=dict)
    skipped_inputs: list[str] = field(default_factory=list)
    """Files / DB tables that were expected but missing — surfaced for the runbook."""


# ── Public API ──────────────────────────────────────────────────────────────


# Map Esby's ``stakeholder_class`` → autonomy_override default. The full
# stakeholder class taxonomy lives in stakeholder_classes.yaml and gets
# imported as a Thought separately for reference.
_NEVER_AUTONOMOUS_CLASSES = {
    "principal_client",
    "reporter",
    "family_member",
    "unknown_external",
}


# Esby policy -> (skill_tags, human-readable correction)
def _policy_to_skill_tags(rule_name: str, match_json: str) -> list[str]:
    """Infer skill tags from the Esby policy rule's match clause.

    send_email rules → email-composer; restructure_vault → general; etc."""
    try:
        match = json.loads(match_json)
    except (TypeError, json.JSONDecodeError):
        match = {}
    action = match.get("action_type", "")
    workflow = match.get("workflow_name", "")
    if action == "send_email" or workflow == "email_reply":
        return ["email-composer", "general"]
    if action in ("restructure_vault", "major_system_refactor"):
        return ["general"]
    return ["general"]


def migrate_esby_install(
    install_root: str | Path,
    *,
    settings_preset: str = "balanced",
    include_old_vault: bool = False,
) -> MigratedState:
    """Read an Esby installed-chief-of-staff directory; produce a MigratedState.

    Args:
        install_root: Path to the ``installed-chief-of-staff/`` directory.
        settings_preset: Which preset to embed in the settings overlay.
            Default ``balanced`` — Esby's autonomy-matrix is conservative
            ("draft_only_or_approval_required" out of the box) so balanced
            is a faithful default.
        include_old_vault: Also chunk ``../.old EsbyVault/Esby/`` markdown
            files into Thoughts. Off by default — the active state is the
            sqlite + configs.

    Returns:
        MigratedState. Pass to ``to_backup_payload()`` for the JSON dict.
    """
    root = Path(install_root).expanduser().resolve()
    state = MigratedState()

    # 1. SQLite — people + policy_rules
    db_path = root / "state" / "chief_of_staff.sqlite"
    if db_path.exists():
        _import_sqlite(state, db_path)
    else:
        logger.warning("esby sqlite missing: %s", db_path)
        state.skipped_inputs.append(str(db_path.relative_to(root)))

    # 2. YAML configs
    config_dir = root / "config"
    if config_dir.is_dir():
        _import_yaml_configs(state, config_dir, settings_preset=settings_preset)
    else:
        state.skipped_inputs.append("config/")

    # 3. setup-report.md → 1 Thought
    setup_report = root / "setup-report.md"
    if setup_report.exists():
        thoughts, sources = chunk_markdown_to_thoughts(
            text=setup_report.read_text(),
            source_uri="setup-report.md",
            source_kind="esby_setup",
            extra_metadata={"migration": "esby_install"},
        )
        state.thoughts.extend(thoughts)
        state.sources.extend(sources)

    # 4. Optional: old vault markdown chunks
    if include_old_vault:
        old_vault = root.parent / ".old EsbyVault" / "Esby"
        if old_vault.is_dir():
            _import_old_vault_markdown(state, old_vault)
        else:
            logger.info("old vault not found at %s; skipping", old_vault)
            state.skipped_inputs.append(str(old_vault.relative_to(root.parent)))

    # 5. Settings overlay (preset already chosen above; just ensure it lands)
    if "autonomy" not in state.settings_overlay:
        state.settings_overlay["autonomy"] = {"default_policy": settings_preset}

    logger.info(
        "esby-install migration: %d people, %d learning rules, %d thoughts, %d skipped",
        len(state.people),
        len(state.learning_rules),
        len(state.thoughts),
        len(state.skipped_inputs),
    )
    return state


def to_backup_payload(state: MigratedState) -> dict[str, Any]:
    """Convert MigratedState to the backup-JSON shape."""
    return build_backup_payload(
        migration_source="esby_install",
        people=state.people,
        learning_rules=state.learning_rules,
        thoughts=state.thoughts,
        sources=state.sources,
        settings_overlay=state.settings_overlay or None,
    )


@dataclass
class EsbyInstallMigration:
    """Convenience wrapper: instantiate, call ``run()`` to get the payload."""

    install_root: str | Path
    settings_preset: str = "balanced"
    include_old_vault: bool = False

    def run(self) -> dict[str, Any]:
        state = migrate_esby_install(
            self.install_root,
            settings_preset=self.settings_preset,
            include_old_vault=self.include_old_vault,
        )
        return to_backup_payload(state)


# ── SQLite extraction ──────────────────────────────────────────────────────


def _import_sqlite(state: MigratedState, db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # ── people ─────────────────────────────────────────────────────────
        cur = conn.execute("SELECT * FROM people")
        for row in cur.fetchall():
            person = _person_from_esby_row(row)
            state.people.append(person)

        # ── policy_rules ───────────────────────────────────────────────────
        cur = conn.execute("SELECT * FROM policy_rules WHERE enabled=1")
        for row in cur.fetchall():
            rule = _learning_rule_from_esby_policy(row)
            state.learning_rules.append(rule)
    finally:
        conn.close()


def _person_from_esby_row(row: sqlite3.Row) -> _PersonRow:
    """Convert an Esby people row into our Person row.

    Esby's ``never_autonomous_send`` flag is honored verbatim. The
    ``autonomy_override`` column in Esby is free-form text ('inherit'
    in the live data); map known values, fall back to inherit otherwise."""
    autonomy = AutonomyOverride.inherit
    raw_override = (row["autonomy_override"] or "").strip().lower()
    if raw_override in {a.value for a in AutonomyOverride}:
        autonomy = AutonomyOverride(raw_override)

    # Some stakeholder classes default to never_autonomous_send even when
    # the row's flag is False — preserve the explicit DB value, but record
    # the implicit default in metadata for transparency.
    nas = bool(row["never_autonomous_send"])

    contact_methods: dict[str, Any] = {}
    # Esby has a person_emails table (0 rows in the live source) — left
    # for a follow-up sprint. For now, no emails imported.

    return _PersonRow(
        id=new_id(),
        name=row["name"],
        organization=row["organization"] or None,
        role=row["role"] or None,
        stakeholder_class=row["stakeholder_class"] or "unknown_external",
        autonomy_override=autonomy,
        relationship_intensity=row["relationship_intensity"],
        response_sla=row["response_sla"] or None,
        never_autonomous_send=nas,
        sensitive_memory_flag=False,  # not in Esby schema
        contact_methods=contact_methods,
        notes_path=row["notes_path"] or None,
        metadata_json={
            "migration": "esby_install",
            "source_id": row["id"],
            "tone_profile": row["tone_profile"],
            "implicit_no_autonomous_send_class": (
                row["stakeholder_class"] in _NEVER_AUTONOMOUS_CLASSES
            ),
        },
    )


def _learning_rule_from_esby_policy(row: sqlite3.Row) -> _LearningRuleRow:
    """Convert an Esby policy_rule row into a LearningRule.

    The mapping is intentionally lossy — Esby's
    ``approval_required at confidence_threshold=0.9`` semantics don't have
    a 1:1 LearningRule equivalent. Encode the rule as natural-language
    guidance the LLM can follow + tag with the relevant skill(s)."""
    decision = (row["decision"] or "").strip()
    rule_name = row["rule_name"]
    scope = row["scope"]
    match_json = row["match_json"]
    confidence = row["confidence_threshold"]
    reversible_only = bool(row["reversible_only"])

    correction = _format_policy_as_correction(
        rule_name=rule_name,
        decision=decision,
        match_json=match_json,
        confidence=confidence,
        reversible_only=reversible_only,
    )
    skill_tags = _policy_to_skill_tags(rule_name, match_json)

    return _LearningRuleRow(
        id=new_id(),
        correction=correction,
        skill_tags=skill_tags,
        source=f"esby-policy:{rule_name}",
        context=f"scope={scope}; match={match_json}",
        notes=(
            f"Migrated from Esby policy_rule.{rule_name}. "
            f"Original decision: {decision}. "
            f"Confidence threshold: {confidence}."
        ),
    )


def _format_policy_as_correction(
    *,
    rule_name: str,
    decision: str,
    match_json: str,
    confidence: float | None,
    reversible_only: bool,
) -> str:
    """Render an Esby policy as a natural-language correction string."""
    try:
        match = json.loads(match_json) or {}
    except (TypeError, json.JSONDecodeError):
        match = {}

    pieces: list[str] = []
    sclass = match.get("stakeholder_class")
    if isinstance(sclass, list):
        sclass_str = " or ".join(sclass)
    elif sclass:
        sclass_str = str(sclass)
    else:
        sclass_str = ""

    action = match.get("action_type") or match.get("workflow_name", "")
    if sclass_str and action:
        pieces.append(
            f"For {action} actions involving {sclass_str} stakeholders:"
        )
    elif sclass_str:
        pieces.append(f"For actions involving {sclass_str} stakeholders:")
    elif action:
        pieces.append(f"For {action} actions:")
    else:
        pieces.append("Default policy:")

    if decision == "approval_required":
        pieces.append("require explicit human approval before executing.")
    elif decision == "draft_only":
        pieces.append("produce a draft only — never autonomous send/execute.")
    else:
        pieces.append(f"apply Esby policy '{decision}'.")

    if confidence:
        pieces.append(f"(Original threshold: confidence ≥ {confidence}.)")
    if reversible_only:
        pieces.append("Restrict to reversible actions only.")
    return " ".join(pieces)


# ── YAML extraction ────────────────────────────────────────────────────────


# Map preference key → (settings dotted path, value mapper). Only keys
# that translate cleanly land in agent.yml; the rest become Thoughts.
_PREFERENCE_TO_SETTINGS: dict[str, str | None] = {
    # autonomy_bias mapping: option_a=cautious, option_b=balanced, option_c=aggressive
    "autonomy_bias": "autonomy.default_policy",
    # other preferences don't have direct settings counterparts yet
    "memory_retention": None,
    "escalation_style": None,
    "calendar_protection": None,
    "content_support_style": None,
    "vault_rewrite_authority": None,
    "review_depth": None,
    "relationship_memory_depth": None,
    "novelty_sensitivity": None,
    "privacy_strictness": None,
    "exception_alerting": None,
}

_AUTONOMY_BIAS_MAP = {
    "option_a": "cautious",
    "option_b": "balanced",
    "option_c": "aggressive",
}


def _import_yaml_configs(
    state: MigratedState, config_dir: Path, *, settings_preset: str
) -> None:
    """Load each YAML config; map what we can to settings, dump the rest as Thoughts."""
    for yaml_path in sorted(config_dir.glob("*.yaml")):
        try:
            text = yaml_path.read_text()
            data = yaml.safe_load(text) or {}
        except (OSError, yaml.YAMLError) as e:
            logger.warning("could not read %s: %s", yaml_path, e)
            state.skipped_inputs.append(str(yaml_path.name))
            continue

        rel = f"config/{yaml_path.name}"

        # Special handling per file
        if yaml_path.name == "preferences.yaml":
            _apply_preferences(state, data, settings_preset=settings_preset)
        elif yaml_path.name == "autonomy-matrix.yaml":
            _apply_autonomy_matrix(state, data)
        # Always store the full config as a Thought for reference recall.
        thought_id = new_id()
        state.thoughts.append(
            _ThoughtRow(
                id=thought_id,
                content=text.strip(),
                fingerprint=_fingerprint(text),
                metadata_json={
                    "migration": "esby_install",
                    "config_file": yaml_path.name,
                },
            )
        )
        state.sources.append(
            _SourceRow(
                thought_id=thought_id,
                source_kind="esby_config",
                source_uri=rel,
                source_title=yaml_path.name,
            )
        )


def _apply_preferences(
    state: MigratedState, data: dict, *, settings_preset: str
) -> None:
    prefs = data.get("preferences", {}) if isinstance(data, dict) else {}
    autonomy_bias = prefs.get("autonomy_bias", {}).get("default")
    if autonomy_bias and autonomy_bias in _AUTONOMY_BIAS_MAP:
        # Esby's preference wins over the CLI --preset arg if explicit
        state.settings_overlay.setdefault("autonomy", {})["default_policy"] = (
            _AUTONOMY_BIAS_MAP[autonomy_bias]
        )


def _apply_autonomy_matrix(state: MigratedState, data: dict) -> None:
    """Esby's autonomy-matrix.yaml encodes 'never_autonomous_send_default'
    classes — these don't have a direct settings counterpart but should be
    surfaced. For now, they live in the imported Thought; per-class
    autonomy lands properly when we add stakeholder-class settings (Sprint
    14)."""
    # No-op for now — the matrix is preserved as a Thought via the
    # general YAML import.
    return


def _import_old_vault_markdown(state: MigratedState, old_vault: Path) -> None:
    """Walk the .old EsbyVault directory; chunk every .md into Thoughts."""
    for md in old_vault.rglob("*.md"):
        try:
            text = md.read_text()
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("could not read %s: %s", md, e)
            continue
        rel = str(md.relative_to(old_vault.parent))
        thoughts, sources = chunk_markdown_to_thoughts(
            text=text,
            source_uri=rel,
            source_kind="old_esby_vault",
            extra_metadata={
                "migration": "esby_install",
                "vault": "old_esby",
            },
        )
        state.thoughts.extend(thoughts)
        state.sources.extend(sources)


def _fingerprint(text: str) -> str:
    """Local re-export for the YAML-as-Thought import path."""
    from agent_core.migrations._helpers import fingerprint_of

    return fingerprint_of(text)


__all__ = [
    "EsbyInstallMigration",
    "MigratedState",
    "migrate_esby_install",
    "to_backup_payload",
]
