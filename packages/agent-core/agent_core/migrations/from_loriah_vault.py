"""Migrate Loriah's Obsidian vault state into a fresh dcos-agent install.

Reads three markdown files in the vault:

  Admin/Loriah Skills/context-loader/operational-state.md
  Admin/Loriah Skills/context-loader/conversation-journal.md
  Admin/Loriah Skills/learning-log/learning-log-data.md

…and produces:

  - **Thoughts** in OpenBrain — every section (split by ## or ### headings)
    becomes a Thought row with source provenance (source_kind="vault",
    source_uri=relative path, source_title=heading text).

  - **Obligations** — a small *seed set* of obvious ones extracted from
    the journal's "Active Threads" + the operational-state's "Flagged
    Items". Conservative: every seeded obligation lands in the inbox so
    the user reviews + promotes them rather than the agent acting on
    something the migration mis-extracted.

  - **Settings overlay** — defaults a fresh install would get, plus the
    vault path so the watcher works out of the box.

Backup-payload assembly + markdown chunking + dict serialization live in
``agent_core.migrations._helpers`` (shared with the Esby migration).

Migration safety:
    - Read-only against the vault. We never write back.
    - Idempotent at the JSON level (modulo created_at timestamps).
    - Conservative on Obligation creation. Every seed obligation has a
      hard-coded title + body; nothing auto-extracted that could surprise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.migrations._helpers import (
    _ObligationRow,
    _SourceRow,
    _ThoughtRow,
    build_backup_payload,
    chunk_markdown_to_thoughts,
    new_id,
)
from agent_core.state.models import (
    ObligationOwner,
    ObligationSource,
    ObligationStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class MigratedState:
    """Everything the migration extracted, ready to be turned into a backup."""

    obligations: list[_ObligationRow] = field(default_factory=list)
    thoughts: list[_ThoughtRow] = field(default_factory=list)
    sources: list[_SourceRow] = field(default_factory=list)
    settings_overlay: dict[str, Any] = field(default_factory=dict)
    skipped_files: list[str] = field(default_factory=list)


# Files we expect inside the vault, relative to vault root.
DEFAULT_VAULT_PATHS = {
    "operational_state": "Admin/Loriah Skills/context-loader/operational-state.md",
    "conversation_journal": "Admin/Loriah Skills/context-loader/conversation-journal.md",
    "learning_log": "Admin/Loriah Skills/learning-log/learning-log-data.md",
}

# Seed Obligations the migration knows how to extract. Hard-coded so the user
# always knows exactly what gets created.
SEED_OBLIGATIONS = [
    {
        "title": "Review CMS Board Meeting Evaluation final version",
        "body": (
            "Esby has iterated through v3, v5, v6 after corruption + missing-agenda-"
            "items feedback. Latest version awaiting AJ approve / request-changes."
        ),
        "owner": ObligationOwner.principal,
        "status": ObligationStatus.in_progress,
        "priority": 5,
        "completion_criteria": [{"type": "principal_ratification"}],
        "derived_from": "conversation-journal.md > Active Threads > CMS Board Meeting Evaluation",
    },
    {
        "title": "Confirm May 11 dinner plans with Charlotte Grinberg",
        "body": (
            "Charlotte proposed May 11 dinner; family free 5:30–7:30pm; can provide "
            "dinner for kids. AJ acknowledged interest. Needs final confirmation + "
            "calendar entry."
        ),
        "owner": ObligationOwner.principal,
        "status": ObligationStatus.inbox,
        "priority": 3,
        "completion_criteria": [{"type": "principal_ratification"}],
        "derived_from": "conversation-journal.md > Active Threads > Charlotte Grinberg Social Coordination",
    },
    {
        "title": "Track Charlotte Grinberg in People notes",
        "body": (
            "(610) 724-3226 SMS via Google Voice. Friend/family; proposing May 11 "
            "dinner coordination. Create People note if not present."
        ),
        "owner": ObligationOwner.agent,
        "status": ObligationStatus.inbox,
        "priority": 2,
        "completion_criteria": [{"type": "principal_ratification"}],
        "derived_from": "learning-log-data.md > People to track",
    },
    {
        "title": "Approve / decline Drive share request for 'Effective Strategic Planning'",
        "body": (
            "drive-shares-dm-noreply@google.com requested permission decision. "
            "Flagged 90% confidence in bootstrap triage batch (2026-04-30)."
        ),
        "owner": ObligationOwner.principal,
        "status": ObligationStatus.inbox,
        "priority": 4,
        "completion_criteria": [{"type": "principal_ratification"}],
        "derived_from": "operational-state.md > Flagged Items Awaiting Action",
    },
]


def migrate_loriah_vault(
    vault_path: str | Path,
    *,
    settings_preset: str = "balanced",
    include_seed_obligations: bool = True,
) -> MigratedState:
    """Read the Loriah vault markdown files; produce a MigratedState."""
    vault = Path(vault_path).expanduser().resolve()
    state = MigratedState()

    for label, rel_path in DEFAULT_VAULT_PATHS.items():
        full = vault / rel_path
        if not full.exists():
            logger.warning("vault file missing: %s", full)
            state.skipped_files.append(rel_path)
            continue
        text = full.read_text()
        thoughts, sources = chunk_markdown_to_thoughts(
            text=text,
            source_uri=rel_path,
            source_kind="vault",
            extra_metadata={"migration": "loriah_vault", "section_label": label},
        )
        state.thoughts.extend(thoughts)
        state.sources.extend(sources)

    if include_seed_obligations:
        for spec in SEED_OBLIGATIONS:
            state.obligations.append(_build_seed_obligation(spec))

    state.settings_overlay = {
        "autonomy": {"default_policy": settings_preset},
        "storage": {"vault_path": str(vault)},
    }

    logger.info(
        "loriah-vault migration: %d thoughts, %d obligations, %d files skipped",
        len(state.thoughts),
        len(state.obligations),
        len(state.skipped_files),
    )
    return state


def to_backup_payload(state: MigratedState) -> dict[str, Any]:
    """Convert MigratedState to the backup-JSON shape."""
    return build_backup_payload(
        migration_source="loriah_vault",
        obligations=state.obligations,
        thoughts=state.thoughts,
        sources=state.sources,
        settings_overlay=state.settings_overlay or None,
    )


@dataclass
class LoriahVaultMigration:
    """Convenience wrapper: instantiate, call ``run()`` to get the payload."""

    vault_path: str | Path
    settings_preset: str = "balanced"
    include_seed_obligations: bool = True

    def run(self) -> dict[str, Any]:
        state = migrate_loriah_vault(
            self.vault_path,
            settings_preset=self.settings_preset,
            include_seed_obligations=self.include_seed_obligations,
        )
        return to_backup_payload(state)


def _build_seed_obligation(spec: dict) -> _ObligationRow:
    return _ObligationRow(
        id=new_id(),
        title=spec["title"],
        body=spec["body"],
        status=spec["status"],
        owner=spec["owner"],
        source=ObligationSource.manual,
        priority=spec.get("priority", 0),
        completion_criteria=list(spec.get("completion_criteria", [])),
    )


__all__ = [
    "DEFAULT_VAULT_PATHS",
    "LoriahVaultMigration",
    "MigratedState",
    "SEED_OBLIGATIONS",
    "migrate_loriah_vault",
    "to_backup_payload",
]
