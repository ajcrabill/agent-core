"""LearningStore — CRUD + JSONL write-ahead for supervised-learning rules.

Esby's working pattern: every rule is appended to ``learnings.jsonl`` (the
write-ahead log) **and** persisted to the database. The JSONL is the portable
export format — humans can read it, migrate it, version it in git. The DB
serves queries.

The two stay in sync because every mutation goes through this class.

API:
  add(correction, skill_tags=['general'], source='', context='', notes='')
  get_by_id(rule_id) → LearningRule | None
  get_by_tag(tag, recent=None, search=None, include_superseded=False)
  list_active(include_superseded=False) → list[LearningRule]
  supersede(old_rule_id, new_correction, source='') → LearningRule
  remove(rule_id) → None  (hard delete; rare — usually supersede)
  stats() → dict (counts by tag, total, superseded count)

Per L13: pre-seeded rule packs become a thin layer on top of this — the
pack just calls add() with a `source='pre-seed:<pack-name>'`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import LearningRule, utcnow

logger = logging.getLogger(__name__)


class LearningStore:
    """The CRUD-and-write-ahead layer over LearningRule.

    Args:
        db: agent-core Database
        jsonl_path: where to append the write-ahead log. If None, defaults to
                    ``~/.local/state/<instance>/learnings.jsonl`` per the
                    XDG-state convention. Pass an explicit path for tests.
        write_ahead: when False, skip the JSONL append (useful for tests
                     that don't care about the WAL).
    """

    def __init__(
        self,
        db: Database,
        *,
        jsonl_path: Path | str | None = None,
        write_ahead: bool = True,
    ) -> None:
        self.db = db
        self.write_ahead = write_ahead
        self.jsonl_path: Path | None
        if write_ahead:
            self.jsonl_path = (
                Path(jsonl_path).expanduser()
                if jsonl_path
                else Path.home() / ".local" / "state" / "agent" / "learnings.jsonl"
            )
        else:
            self.jsonl_path = None

    # ── Mutators ────────────────────────────────────────────────────────────

    def add(
        self,
        correction: str,
        *,
        skill_tags: list[str] | None = None,
        source: str = "",
        context: str = "",
        notes: str = "",
    ) -> LearningRule:
        """Add a new rule. Writes DB row + appends to JSONL.

        ``skill_tags`` defaults to ``["general"]`` (loaded on every decision).
        Empty tags list raises ValueError — every rule must be loadable
        somewhere.
        """
        if skill_tags is None:
            skill_tags = ["general"]
        if not skill_tags:
            raise ValueError("skill_tags must contain at least one tag")

        rule = LearningRule(
            correction=correction,
            skill_tags=skill_tags,
            source=source,
            context=context,
            notes=notes,
        )
        with self.db.session() as s:
            s.add(rule)
            s.commit()
            s.refresh(rule)

        self._append_jsonl(self._rule_to_dict(rule))
        logger.info("learning rule added: id=%s tags=%s", rule.id[:8], skill_tags)
        return rule

    def supersede(
        self,
        old_rule_id: str,
        new_correction: str,
        *,
        source: str = "",
        context: str = "",
        notes: str = "",
        skill_tags: list[str] | None = None,
    ) -> LearningRule:
        """Create a new rule that supersedes ``old_rule_id``.

        Writes the new rule, then marks the old one's ``superseded_by`` to
        point at the new one. Both writes happen in the same transaction.
        Inherits skill_tags from old unless overridden.
        """
        with self.db.session() as s:
            old = s.get(LearningRule, old_rule_id)
            if old is None:
                raise ValueError(f"rule {old_rule_id!r} not found")

            new = LearningRule(
                correction=new_correction,
                skill_tags=skill_tags if skill_tags is not None else list(old.skill_tags or []),
                source=source,
                context=context,
                notes=notes,
            )
            s.add(new)
            s.flush()  # assign id

            old.superseded_by = new.id
            s.add(old)
            s.commit()
            s.refresh(new)
            s.refresh(old)

        self._append_jsonl(self._rule_to_dict(new, supersedes=old_rule_id))
        logger.info(
            "learning rule superseded: old=%s → new=%s",
            old_rule_id[:8],
            new.id[:8],
        )
        return new

    def remove(self, rule_id: str) -> None:
        """Hard delete (no JSONL trail beyond a tombstone line). Use sparingly
        — supersede() is almost always the right move."""
        with self.db.session() as s:
            rule = s.get(LearningRule, rule_id)
            if rule is None:
                raise ValueError(f"rule {rule_id!r} not found")
            s.delete(rule)
            s.commit()

        self._append_jsonl({"_tombstone": rule_id, "deleted_at": utcnow().isoformat()})
        logger.info("learning rule removed: id=%s", rule_id[:8])

    # ── Read API ────────────────────────────────────────────────────────────

    def get_by_id(self, rule_id: str) -> LearningRule | None:
        with self.db.session() as s:
            return s.get(LearningRule, rule_id)

    def get_by_tag(
        self,
        tag: str,
        *,
        recent: int | None = None,
        search: str | None = None,
        include_superseded: bool = False,
    ) -> list[LearningRule]:
        """Active rules tagged with ``tag`` (newest first).

        ``recent`` caps to N most-recent. ``search`` is a substring match on
        the correction text (case-insensitive). ``include_superseded`` drops
        the default filter (ordinarily we hide superseded rules)."""
        with self.db.session() as s:
            stmt = select(LearningRule)
            if not include_superseded:
                stmt = stmt.where(LearningRule.superseded_by.is_(None))
            stmt = stmt.order_by(LearningRule.created_at.desc())
            rows = list(s.exec(stmt).all())

        # JSON column filtering in Python (portable across backends)
        rows = [r for r in rows if tag in (r.skill_tags or [])]
        if search:
            needle = search.lower()
            rows = [r for r in rows if needle in r.correction.lower()]
        if recent is not None:
            rows = rows[:recent]
        return rows

    def list_active(self, *, include_superseded: bool = False) -> list[LearningRule]:
        """All rules. Default: only active (non-superseded)."""
        with self.db.session() as s:
            stmt = select(LearningRule)
            if not include_superseded:
                stmt = stmt.where(LearningRule.superseded_by.is_(None))
            stmt = stmt.order_by(LearningRule.created_at.desc())
            return list(s.exec(stmt).all())

    def stats(self) -> dict:
        """Aggregate counts. Used by the maintenance / weekly-review surfaces."""
        with self.db.session() as s:
            all_rules = list(s.exec(select(LearningRule)).all())
        active = [r for r in all_rules if r.superseded_by is None]
        superseded = len(all_rules) - len(active)
        by_tag: dict[str, int] = {}
        for r in active:
            for t in r.skill_tags or []:
                by_tag[t] = by_tag.get(t, 0) + 1
        return {
            "total": len(all_rules),
            "active": len(active),
            "superseded": superseded,
            "by_tag": by_tag,
        }

    # ── Bulk import (for migrating Loriah/Esby's existing learnings.jsonl) ──

    def import_jsonl(
        self,
        source_path: str | Path,
        *,
        source_marker: str | None = None,
        skip_existing_ids: bool = True,
    ) -> int:
        """Append rules from an existing JSONL file (e.g., Esby's
        ``learnings.jsonl``). Returns count imported."""
        source_path = Path(source_path).expanduser()
        with self.db.session() as s:
            existing_ids = (
                {r.id for r in s.exec(select(LearningRule)).all()} if skip_existing_ids else set()
            )

        imported = 0
        with open(source_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("_tombstone"):
                    continue
                if skip_existing_ids and record.get("id") in existing_ids:
                    continue

                rule = LearningRule(
                    id=record.get("id"),  # preserve original id
                    correction=record.get("correction", ""),
                    skill_tags=self._normalize_tags(record.get("skill_tags")),
                    source=record.get("source", "")
                    + (f" (imported from {source_marker})" if source_marker else ""),
                    context=record.get("context", ""),
                    notes=record.get("notes", ""),
                    superseded_by=record.get("superseded_by"),
                )
                with self.db.session() as s:
                    s.add(rule)
                    s.commit()
                imported += 1

        logger.info("imported %d rules from %s", imported, source_path)
        return imported

    # ── Internals ───────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_tags(value) -> list[str]:  # type: ignore[no-untyped-def]
        """Esby's JSONL stores tags as comma-separated string OR list. Normalize."""
        if value is None:
            return ["general"]
        if isinstance(value, list):
            return [str(t).strip() for t in value if str(t).strip()]
        if isinstance(value, str):
            return [t.strip() for t in value.split(",") if t.strip()]
        return ["general"]

    def _append_jsonl(self, record: dict) -> None:
        if not self.write_ahead or self.jsonl_path is None:
            return
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    @staticmethod
    def _rule_to_dict(rule: LearningRule, *, supersedes: str | None = None) -> dict:
        d: dict = {
            "id": rule.id,
            "timestamp": rule.created_at.isoformat() if rule.created_at else None,
            "correction": rule.correction,
            "skill_tags": list(rule.skill_tags or []),
            "source": rule.source,
            "context": rule.context,
            "notes": rule.notes,
            "superseded_by": rule.superseded_by,
        }
        if supersedes:
            d["supersedes"] = supersedes
        return d


# ── Helpers exposed for tests / consumers ────────────────────────────────────


def jsonl_export(
    store: LearningStore, dest: Path | str, *, include_superseded: bool = False
) -> int:
    """Write the active rules out as a JSONL file (one record per line)."""
    rules: Iterable[LearningRule] = store.list_active(include_superseded=include_superseded)
    dest = Path(dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(dest, "w", encoding="utf-8") as f:
        for rule in rules:
            f.write(json.dumps(LearningStore._rule_to_dict(rule), sort_keys=True) + "\n")
            count += 1
    return count


__all__ = ["LearningStore", "jsonl_export"]
