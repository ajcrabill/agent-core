"""Vault renderer — projects database state into markdown files for human view.

The database is the source of truth. The vault is a generated projection that
lives where the user can browse it (Obsidian for dCoS, MkDocs for iKB).

What we render in this commit (Sprint 1.4):
  - ObligationBoard: 4-column kanban (inbox/in-progress/waiting/done) with one
    .md file per obligation in the corresponding folder
  - Learning rules: single Learning-Rules.md listing active rules grouped by tag

What we'll add in later sprints:
  - Delegations tracker (when delegations get used)
  - Conversation journal (Sprint 2 — but it's bidirectional, see watcher)
  - Run-log dailies + action-log weekly digests (Sprint 4.5)
  - Per-skill content-creation surfaces (Sprint 5c)

Idempotency: every write checks whether the on-disk content already matches
what we'd write, and skips the write if so. This prevents spurious file
mtime updates that would otherwise wake the watcher (Sprint 1.5) for no reason.

Stale-file cleanup: render_obligation_board() deletes files in the kanban
columns that don't correspond to a current obligation. Same for rules.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import (
    LearningRule,
    Obligation,
    ObligationStatus,
)

logger = logging.getLogger(__name__)


# ── Filename helpers ─────────────────────────────────────────────────────────


_SLUG_REPLACE = re.compile(r"[^a-z0-9]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")


def slugify(text: str, *, max_len: int = 60) -> str:
    """Filesystem-safe slug for use in filenames.

    Lowercases, replaces non-alphanumerics with single dashes, trims edges,
    and truncates. Empty result yields 'untitled'.
    """
    s = _SLUG_REPLACE.sub("-", text.lower())
    s = _SLUG_TRIM.sub("", s)
    if not s:
        return "untitled"
    return s[:max_len].rstrip("-") or "untitled"


def obligation_filename(ob: Obligation) -> str:
    """Stable filename for an obligation: ``<YYYY-MM-DD>-<slug>-<id8>.md``.

    Includes a short id suffix to disambiguate same-day same-title obligations
    deterministically (so the renderer never has to invent uniqueness at runtime).
    """
    date = ob.created_at.date().isoformat()
    slug = slugify(ob.title)
    short_id = ob.id[:8]
    return f"{date}-{slug}-{short_id}.md"


# ── Rendering result ─────────────────────────────────────────────────────────


@dataclass
class RenderResult:
    """Per-render-call summary, useful for logging + tests."""

    written: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)

    @property
    def total_changed(self) -> int:
        return len(self.written) + len(self.deleted)


# ── Idempotent file write ────────────────────────────────────────────────────


def _idempotent_write(path: Path, content: str, result: RenderResult) -> None:
    """Write content to path only if it would change. Updates result lists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            result.unchanged.append(path)
            return
    path.write_text(content, encoding="utf-8")
    result.written.append(path)


# ── Markdown serialization ───────────────────────────────────────────────────


def _yaml_block(data: dict) -> str:
    """Render a frontmatter block. Stable key order (sort_keys=False but the
    caller controls insertion order)."""
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False).rstrip()


def render_obligation_md(ob: Obligation) -> str:
    """Render a single obligation to markdown with YAML frontmatter."""
    fm: dict = {
        "id": ob.id,
        "status": ob.status.value,
        "owner": ob.owner.value,
        "source": ob.source.value,
        "priority": ob.priority,
    }
    if ob.due_at:
        fm["due_at"] = ob.due_at.isoformat()
    fm["created_at"] = ob.created_at.isoformat()
    if ob.parent_id:
        fm["parent_id"] = ob.parent_id
    if ob.completion_criteria:
        fm["completion_criteria"] = ob.completion_criteria

    parts = [
        "---",
        _yaml_block(fm),
        "---",
        "",
        f"# {ob.title}",
        "",
    ]
    if ob.body:
        parts.append(ob.body.rstrip())
        parts.append("")
    return "\n".join(parts)


def render_learning_rules_md(rules: Iterable[LearningRule]) -> str:
    """Render the consolidated learning-rules markdown.

    Groups by skill tag (general first, then alphabetical), drops superseded
    rules (only active rules surface in the rendered file).
    """
    active = [r for r in rules if r.superseded_by is None]

    # Group by tag — a rule with multiple tags appears under each.
    by_tag: dict[str, list[LearningRule]] = {}
    for rule in active:
        for tag in rule.skill_tags or ["general"]:
            by_tag.setdefault(tag, []).append(rule)

    # Order: 'general' first, then alphabetical
    ordered_tags = sorted(by_tag.keys(), key=lambda t: (t != "general", t))

    # Note: NO `rendered_at` timestamp in frontmatter — it would defeat
    # idempotency. File mtime carries the same info if needed.
    parts = [
        "---",
        _yaml_block(
            {
                "type": "learning-rules",
                "active_rule_count": len(active),
            }
        ),
        "---",
        "",
        "# Learning Rules",
        "",
        "_Generated from the database. Edit rules via the agent — direct edits "
        "to this file are NOT synced back._",
        "",
    ]

    if not active:
        parts.append("_No active learning rules yet._")
        parts.append("")
        return "\n".join(parts)

    for tag in ordered_tags:
        parts.append(f"## {tag}")
        parts.append("")
        # Stable order within a tag: most recent first
        for rule in sorted(by_tag[tag], key=lambda r: r.created_at, reverse=True):
            parts.append(f"- **{rule.correction}**")
            meta_bits = []
            if rule.source:
                meta_bits.append(f"source: {rule.source}")
            meta_bits.append(f"id: `{rule.id[:8]}`")
            meta_bits.append(f"added: {rule.created_at.date().isoformat()}")
            parts.append("  - " + " · ".join(meta_bits))
            if rule.context:
                parts.append(f"  - context: {rule.context}")
        parts.append("")

    return "\n".join(parts)


# ── Renderer ─────────────────────────────────────────────────────────────────


# Mapping from db status enum → folder name on disk.
# Matches the Sprint 0 OB consolidation: inbox / in-progress / waiting / done.
_STATUS_TO_DIR = {
    ObligationStatus.inbox: "inbox",
    ObligationStatus.in_progress: "in-progress",
    ObligationStatus.waiting: "waiting",
    ObligationStatus.done: "done",
}


class VaultRenderer:
    """Project database state into markdown files in the vault.

    Each render method is idempotent and reports a RenderResult.
    """

    def __init__(self, db: Database, vault_path: Path | str) -> None:
        self.db = db
        self.vault_path = Path(vault_path)

    # ── Path helpers ────────────────────────────────────────────────────────

    def obligation_board_dir(self) -> Path:
        return self.vault_path / "ObligationBoard"

    def column_dir(self, status: ObligationStatus) -> Path:
        return self.obligation_board_dir() / _STATUS_TO_DIR[status]

    def learning_rules_path(self) -> Path:
        return self.vault_path / "Learning-Rules.md"

    # ── Rendering ───────────────────────────────────────────────────────────

    def render_obligation_board(self) -> RenderResult:
        """Render the full ObligationBoard, write changed files, prune stale.

        Stale = a file in one of the four column dirs whose stem doesn't match
        any current obligation. Only sweeps files that look like rendered
        obligations (filename ends in -<8hex>.md), so user-added files in
        adjacent paths are left alone.
        """
        result = RenderResult()

        with self.db.session() as s:
            obligations = list(s.exec(select(Obligation)).all())

        # Build the set of files we expect to exist after rendering, keyed by
        # their full path so we can compare against what's on disk.
        expected_paths: set[Path] = set()
        for ob in obligations:
            target = self.column_dir(ob.status) / obligation_filename(ob)
            expected_paths.add(target)
            content = render_obligation_md(ob)
            _idempotent_write(target, content, result)

        # Sweep stale files in any of the 4 column dirs.
        for status in ObligationStatus:
            col_dir = self.column_dir(status)
            if not col_dir.exists():
                continue
            for f in col_dir.glob("*.md"):
                if not _looks_like_rendered_obligation(f):
                    continue  # leave user files alone
                if f in expected_paths:
                    continue
                f.unlink()
                result.deleted.append(f)

        logger.info(
            "obligation_board: %d written, %d unchanged, %d deleted",
            len(result.written),
            len(result.unchanged),
            len(result.deleted),
        )
        return result

    def render_learning_rules(self) -> RenderResult:
        """Render the consolidated Learning-Rules.md."""
        result = RenderResult()
        with self.db.session() as s:
            rules = list(s.exec(select(LearningRule)).all())
        content = render_learning_rules_md(rules)
        _idempotent_write(self.learning_rules_path(), content, result)
        return result

    def render_all(self) -> RenderResult:
        """Render every supported surface. Aggregates a single RenderResult."""
        agg = RenderResult()
        for sub in (self.render_obligation_board(), self.render_learning_rules()):
            agg.written.extend(sub.written)
            agg.unchanged.extend(sub.unchanged)
            agg.deleted.extend(sub.deleted)
        return agg


# ── Stale-file detection ─────────────────────────────────────────────────────

# Filename ends in '-<8 hex>.md' — matches our obligation_filename() output.
_RENDERED_NAME = re.compile(r"-[0-9a-f]{8}\.md$")


def _looks_like_rendered_obligation(path: Path) -> bool:
    return bool(_RENDERED_NAME.search(path.name))


__all__ = [
    "RenderResult",
    "VaultRenderer",
    "obligation_filename",
    "render_learning_rules_md",
    "render_obligation_md",
    "slugify",
]
