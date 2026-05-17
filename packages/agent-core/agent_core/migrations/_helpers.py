"""Shared internals for migration modules.

Both ``from_loriah_vault`` and ``from_esby_install`` need:

  - the same Thought/Source/Obligation/Person row dataclasses
  - the same markdown chunker (split by ##/### headings, strip frontmatter)
  - the same dict serializers that produce ``restore_backup``-compatible JSON
    (which includes the SAEnum-name vs enum-value gotcha)
  - the same fingerprint algorithm so dedup works across migrations
  - the same backup-payload shape

Lifted here so a single source of truth handles those shared concerns.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml

from agent_core.ops.backup import FORMAT_VERSION
from agent_core.state.models import (
    AutonomyOverride,
    ObligationOwner,
    ObligationSource,
    ObligationStatus,
)

# ── Internal pre-row dataclasses ───────────────────────────────────────────


@dataclass
class _ThoughtRow:
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
class _PersonRow:
    id: str
    name: str
    organization: str | None = None
    role: str | None = None
    stakeholder_class: str = "unknown_external"
    autonomy_override: AutonomyOverride = AutonomyOverride.inherit
    relationship_intensity: int | None = None
    response_sla: str | None = None
    never_autonomous_send: bool = False
    sensitive_memory_flag: bool = False
    contact_methods: dict[str, Any] = field(default_factory=dict)
    notes_path: str | None = None
    metadata_json: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _LearningRuleRow:
    id: str
    correction: str
    skill_tags: list[str] = field(default_factory=list)
    source: str = "migration"
    context: str = ""
    notes: str = ""
    superseded_by_id: str | None = None
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ── Markdown chunker ───────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)
_MIN_SECTION_LEN = 30


def split_by_headings(text: str) -> list[tuple[str, str]]:
    """Split text by ##/### headings → list of (heading, body) tuples.

    Pre-heading preamble (if any) lands under heading=''. Body of each
    section excludes the heading line itself."""
    matches = list(_HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    if not matches:
        return [("", text)]
    if matches[0].start() > 0:
        sections.append(("", text[: matches[0].start()]))
    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        sections.append((heading, body))
    return sections


def strip_frontmatter(text: str) -> str:
    """Drop YAML frontmatter (``---\\n…\\n---\\n`` at the top)."""
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip()


def chunk_markdown_to_thoughts(
    *,
    text: str,
    source_uri: str,
    source_kind: str = "vault",
    extra_metadata: dict | None = None,
    min_section_len: int = _MIN_SECTION_LEN,
) -> tuple[list[_ThoughtRow], list[_SourceRow]]:
    """Chunk a markdown document into Thoughts + their provenance Sources.

    Sections shorter than ``min_section_len`` chars are dropped (typically
    navigation headers, not content)."""
    text = strip_frontmatter(text)
    sections = split_by_headings(text)
    thoughts: list[_ThoughtRow] = []
    sources: list[_SourceRow] = []
    for heading, body in sections:
        body = body.strip()
        if len(body) < min_section_len:
            continue
        thought_id = new_id()
        meta = dict(extra_metadata or {})
        meta["section_heading"] = heading
        thoughts.append(
            _ThoughtRow(
                id=thought_id,
                content=body,
                fingerprint=fingerprint_of(body),
                metadata_json=meta,
            )
        )
        sources.append(
            _SourceRow(
                thought_id=thought_id,
                source_kind=source_kind,
                source_uri=source_uri,
                source_title=heading or None,
            )
        )
    return thoughts, sources


# ── Helpers ─────────────────────────────────────────────────────────────────


def new_id() -> str:
    return str(uuid.uuid4())


def fingerprint_of(content: str) -> str:
    """Same fingerprint algorithm as openbrain.store._fingerprint so dedup
    indexes line up if the user later re-captures the same content."""
    normalized = " ".join(content.split()).lower().strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ── Backup-payload serialization ──────────────────────────────────────────


# IMPORTANT: SAEnum(native_enum=False) stores the enum's *name*, not value
# (so "in_progress" not "in-progress"). Migrations must use .name to match
# what create_backup() reads back via reflection. Using .value here would
# write strings the reflection-based restore can't decode.


def obligation_to_dict(o: _ObligationRow) -> dict[str, Any]:
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


def thought_to_dict(t: _ThoughtRow) -> dict[str, Any]:
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


def source_to_dict(s: _SourceRow) -> dict[str, Any]:
    return {
        "id": None,  # auto-increment; DB assigns on insert
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


def person_to_dict(p: _PersonRow) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "organization": p.organization,
        "role": p.role,
        "stakeholder_class": p.stakeholder_class,
        "autonomy_override": p.autonomy_override.name,
        "relationship_intensity": p.relationship_intensity,
        "response_sla": p.response_sla,
        "never_autonomous_send": p.never_autonomous_send,
        "sensitive_memory_flag": p.sensitive_memory_flag,
        "contact_methods": p.contact_methods or {},
        "notes_path": p.notes_path,
        "metadata_json": p.metadata_json,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


def learning_rule_to_dict(r: _LearningRuleRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "correction": r.correction,
        "skill_tags": r.skill_tags,
        "source": r.source,
        "context": r.context,
        "notes": r.notes,
        "superseded_by_id": r.superseded_by_id,
        "is_active": r.is_active,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


# ── Top-level payload assembly ────────────────────────────────────────────


def build_backup_payload(
    *,
    migration_source: str,
    obligations: list[_ObligationRow] | None = None,
    thoughts: list[_ThoughtRow] | None = None,
    sources: list[_SourceRow] | None = None,
    people: list[_PersonRow] | None = None,
    learning_rules: list[_LearningRuleRow] | None = None,
    settings_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a backup-format JSON dict from migration row collections.

    Tables only land in the payload if they have rows — restore_backup
    skips empty tables, but excluding them keeps the JSON clean.
    """
    obligations = obligations or []
    thoughts = thoughts or []
    sources = sources or []
    people = people or []
    learning_rules = learning_rules or []

    counts: dict[str, int] = {}
    tables: dict[str, list[dict[str, Any]]] = {}
    if obligations:
        tables["obligation"] = [obligation_to_dict(o) for o in obligations]
        counts["obligation"] = len(obligations)
    if thoughts:
        tables["thought"] = [thought_to_dict(t) for t in thoughts]
        counts["thought"] = len(thoughts)
    if sources:
        tables["thought_source"] = [source_to_dict(s) for s in sources]
        counts["thought_source"] = len(sources)
    if people:
        tables["person"] = [person_to_dict(p) for p in people]
        counts["person"] = len(people)
    if learning_rules:
        tables["learning_rule"] = [learning_rule_to_dict(r) for r in learning_rules]
        counts["learning_rule"] = len(learning_rules)

    payload: dict[str, Any] = {
        "manifest": {
            "format_version": FORMAT_VERSION,
            "agent_core_version": "0.0.1",
            "schema_head": None,
            "created_at": datetime.now(UTC).isoformat(),
            "tables": counts,
            "includes_settings": bool(settings_overlay),
            "includes_identity": False,
            "migration_source": migration_source,
        },
        "tables": tables,
    }
    if settings_overlay:
        payload["settings_yaml"] = yaml.safe_dump(
            settings_overlay, sort_keys=True, default_flow_style=False
        )
    return payload


__all__ = [
    "_LearningRuleRow",
    "_ObligationRow",
    "_PersonRow",
    "_SourceRow",
    "_ThoughtRow",
    "build_backup_payload",
    "chunk_markdown_to_thoughts",
    "fingerprint_of",
    "learning_rule_to_dict",
    "new_id",
    "obligation_to_dict",
    "person_to_dict",
    "source_to_dict",
    "split_by_headings",
    "strip_frontmatter",
    "thought_to_dict",
]
