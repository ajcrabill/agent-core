"""Migrate Loriah's Obsidian vault state into a fresh dcos-agent install.

Reads three markdown files in the vault:

  Admin/Loriah Skills/context-loader/operational-state.md
  Admin/Loriah Skills/context-loader/conversation-journal.md
  Admin/Loriah Skills/learning-log/learning-log-data.md

…and produces:

  - **Thoughts** in OpenBrain — every section (split by ## or ### headings)
    becomes a Thought row with source provenance (source_kind="vault",
    source_uri=relative path, source_title=heading text). This preserves
    full context and makes it semantically searchable from day one.

  - **Obligations** — a small *seed set* of obvious ones extracted from
    the journal's "Active Threads" + the operational-state's "Flagged
    Items". Conservative: every seeded obligation lands in the inbox so
    the user reviews + promotes them rather than the agent acting on
    something the migration mis-extracted.

  - **Settings overlay** — defaults a fresh install would get, plus the
    vault path so the watcher works out of the box.

The output dict is the same shape ``ops.create_backup()`` produces, so
``ops.restore_backup()`` consumes it without modification.

Migration safety:
    - Read-only against the vault. We never write back.
    - Idempotent at the JSON level — re-running produces the same payload
      (modulo created_at timestamps). Restoring re-imports cleanly because
      ``restore_backup`` clears tables before inserting.
    - Conservative on Obligation creation. Every seed obligation has a
      hard-coded title + body; nothing is auto-extracted in a way that
      could surprise the user. If the vault has new content that ought to
      become an Obligation, the user can promote it from the imported
      Thoughts manually.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.ops.backup import FORMAT_VERSION
from agent_core.state.models import (
    ObligationOwner,
    ObligationSource,
    ObligationStatus,
)

logger = logging.getLogger(__name__)


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass
class _ThoughtRow:
    """Internal pre-row — turned into a backup-JSON dict by ``to_backup_payload``."""

    id: str
    content: str
    fingerprint: str
    metadata_json: dict | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _SourceRow:
    thought_id: str
    source_kind: str
    source_uri: str | None
    source_title: str | None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _ObligationRow:
    id: str
    title: str
    body: str | None
    status: ObligationStatus
    owner: ObligationOwner
    source: ObligationSource
    priority: int = 0
    completion_criteria: list[dict] = field(default_factory=list)
    parent_id: str | None = None
    due_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class MigratedState:
    """Everything the migration extracted, ready to be turned into a backup."""

    obligations: list[_ObligationRow] = field(default_factory=list)
    thoughts: list[_ThoughtRow] = field(default_factory=list)
    sources: list[_SourceRow] = field(default_factory=list)
    settings_overlay: dict[str, Any] = field(default_factory=dict)
    skipped_files: list[str] = field(default_factory=list)
    """Files that were expected but not found — surfaced so the runbook
    can flag them."""


# ── Public API ──────────────────────────────────────────────────────────────


# Files we expect inside the vault, relative to vault root.
DEFAULT_VAULT_PATHS = {
    "operational_state": "Admin/Loriah Skills/context-loader/operational-state.md",
    "conversation_journal": "Admin/Loriah Skills/context-loader/conversation-journal.md",
    "learning_log": "Admin/Loriah Skills/learning-log/learning-log-data.md",
}

# Seed Obligations the migration knows how to extract. Hard-coded so the user
# always knows exactly what gets created. Each ``derived_from`` field doubles
# as a search hint pointing back at the source markdown chunk.
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
    """Read the Loriah vault markdown files; produce a MigratedState.

    Args:
        vault_path: Root of the Obsidian vault (the dir containing ``Admin/``).
        settings_preset: Which preset to embed in the settings overlay
            (cautious / balanced / aggressive). Default ``balanced``.
        include_seed_obligations: If True, create the curated seed
            Obligations. Set False when you only want the OpenBrain
            Thoughts (e.g., re-running migration after manually creating
            obligations from the imported context).

    Returns:
        MigratedState. Pass to ``to_backup_payload()`` for the JSON dict.
    """
    vault = Path(vault_path).expanduser().resolve()
    state = MigratedState()

    for label, rel_path in DEFAULT_VAULT_PATHS.items():
        full = vault / rel_path
        if not full.exists():
            logger.warning("vault file missing: %s", full)
            state.skipped_files.append(rel_path)
            continue
        text = full.read_text()
        _import_file_as_thoughts(state, text=text, source_uri=rel_path, label=label)

    if include_seed_obligations:
        for spec in SEED_OBLIGATIONS:
            state.obligations.append(_build_seed_obligation(spec))

    state.settings_overlay = _build_settings_overlay(
        preset=settings_preset, vault_path=str(vault)
    )

    logger.info(
        "loriah-vault migration: %d thoughts, %d obligations, %d files skipped",
        len(state.thoughts),
        len(state.obligations),
        len(state.skipped_files),
    )
    return state


def to_backup_payload(state: MigratedState) -> dict[str, Any]:
    """Convert MigratedState to the backup-JSON shape.

    Same layout as ``ops.create_backup`` produces, so ``restore_backup``
    accepts it without modification."""
    counts = {
        "obligation": len(state.obligations),
        "thought": len(state.thoughts),
        "thought_source": len(state.sources),
    }
    payload: dict[str, Any] = {
        "manifest": {
            "format_version": FORMAT_VERSION,
            "agent_core_version": "0.0.1",
            "schema_head": None,  # restore_backup ignores when skip_schema_check
            "created_at": datetime.now(UTC).isoformat(),
            "tables": counts,
            "includes_settings": bool(state.settings_overlay),
            "includes_identity": False,
            "migration_source": "loriah_vault",
        },
        "tables": {
            "obligation": [_obligation_to_dict(o) for o in state.obligations],
            "thought": [_thought_to_dict(t) for t in state.thoughts],
            "thought_source": [_source_to_dict(s) for s in state.sources],
        },
    }
    if state.settings_overlay:
        import yaml  # local — only the migration produces a YAML overlay

        payload["settings_yaml"] = yaml.safe_dump(
            state.settings_overlay, sort_keys=True, default_flow_style=False
        )
    return payload


# ── Convenience class wrapping the two functions ───────────────────────────


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


# ── File → Thoughts ────────────────────────────────────────────────────────


# Match ## or ### headings (skip frontmatter delimiters).
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)


def _import_file_as_thoughts(
    state: MigratedState, *, text: str, source_uri: str, label: str
) -> None:
    """Split a markdown file by ## / ### headings; each section → Thought.

    Sections that are shorter than 30 chars (after stripping) are dropped —
    they're usually navigation headers without content.
    """
    text = _strip_frontmatter(text)
    sections = _split_by_headings(text)
    for heading, body in sections:
        body = body.strip()
        if len(body) < 30:
            continue
        thought_id = _new_id()
        state.thoughts.append(
            _ThoughtRow(
                id=thought_id,
                content=body,
                fingerprint=_fingerprint(body),
                metadata_json={
                    "migration": "loriah_vault",
                    "section_label": label,
                    "section_heading": heading,
                },
            )
        )
        state.sources.append(
            _SourceRow(
                thought_id=thought_id,
                source_kind="vault",
                source_uri=source_uri,
                source_title=heading or label,
            )
        )


def _strip_frontmatter(text: str) -> str:
    """Drop YAML frontmatter (``---\\n…\\n---\\n`` at the top)."""
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip()


def _split_by_headings(text: str) -> list[tuple[str, str]]:
    """Return list of (heading, body) tuples. The pre-heading preamble (if
    any) is stored under heading=''."""
    matches = list(_HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    if not matches:
        return [("", text)]
    # Pre-heading preamble
    if matches[0].start() > 0:
        sections.append(("", text[: matches[0].start()]))
    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        sections.append((heading, body))
    return sections


# ── Seed Obligations ───────────────────────────────────────────────────────


def _build_seed_obligation(spec: dict) -> _ObligationRow:
    return _ObligationRow(
        id=_new_id(),
        title=spec["title"],
        body=spec["body"],
        status=spec["status"],
        owner=spec["owner"],
        source=ObligationSource.manual,
        priority=spec.get("priority", 0),
        completion_criteria=list(spec.get("completion_criteria", [])),
    )


# ── Settings overlay ───────────────────────────────────────────────────────


def _build_settings_overlay(*, preset: str, vault_path: str) -> dict[str, Any]:
    """Minimal overlay — preset choice + vault path. Everything else
    defaults are fine for a fresh install."""
    return {
        "autonomy": {"default_policy": preset},
        "storage": {"vault_path": vault_path},
    }


# ── Helpers ────────────────────────────────────────────────────────────────


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _fingerprint(content: str) -> str:
    """Same fingerprint algorithm as openbrain.store._fingerprint so the
    dedup index lines up if the user later re-captures the same content."""
    import hashlib

    normalized = " ".join(content.split()).lower().strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _obligation_to_dict(o: _ObligationRow) -> dict[str, Any]:
    # SAEnum (native_enum=False) stores the enum's *name*, not its value —
    # using .value here would write "in-progress" but the column holds
    # "in_progress". Match what create_backup() reads back via reflection.
    return {
        "id": o.id,
        "title": o.title,
        "body": o.body,
        "status": o.status.name,
        "owner": o.owner.name,
        "source": o.source.name,
        "parent_id": o.parent_id,
        "completion_criteria": o.completion_criteria,
        "priority": o.priority,
        "due_at": o.due_at.isoformat() if o.due_at else None,
        "started_at": o.started_at.isoformat() if o.started_at else None,
        "completed_at": o.completed_at.isoformat() if o.completed_at else None,
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
    }


def _thought_to_dict(t: _ThoughtRow) -> dict[str, Any]:
    return {
        "id": t.id,
        "content": t.content,
        "fingerprint": t.fingerprint,
        "metadata_json": t.metadata_json,
        "embedding": None,
        "embedding_model": None,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.created_at.isoformat(),
    }


def _source_to_dict(s: _SourceRow) -> dict[str, Any]:
    return {
        # id is auto-incremented; restore inserts and the DB assigns
        "id": None,
        "thought_id": s.thought_id,
        "source_kind": s.source_kind,
        "source_uri": s.source_uri,
        "source_title": s.source_title,
        "valid_from": None,
        "valid_until": None,
        "authority": None,
        "visibility": "all",
        "fetched_at": s.fetched_at.isoformat(),
    }


__all__ = [
    "DEFAULT_VAULT_PATHS",
    "LoriahVaultMigration",
    "MigratedState",
    "SEED_OBLIGATIONS",
    "migrate_loriah_vault",
    "to_backup_payload",
]
