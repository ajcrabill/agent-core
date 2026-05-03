"""Vault watcher — sync human edits in the vault back into the database.

The renderer (`renderer.py`) projects db state into markdown. This module is
the inverse: when a human edits a tracked markdown file in the vault, the
watcher parses the edit and writes back to the db.

Sprint 1.5 scope (MVP):
  - Watch the four ObligationBoard column directories
  - On MODIFIED event for a rendered obligation file:
      → parse frontmatter + body, update the matching obligation row
  - On MOVED event between column directories:
      → update the obligation's status to match the destination column
  - On DELETED: log only. Never auto-delete from the db. (Safe default; users
    can delete obligations explicitly via the chat or OB UI.)
  - On CREATED: ignore. Human-created tasks go via chat in the MVP. (Future:
    parse + create row, gated by a config flag.)

Feedback-loop prevention:
  Renderer is idempotent and only writes when content changes — so the
  watcher's "modified" handler firing immediately after a render is fine,
  because the on-disk content already matches what the db would re-render
  to. We additionally do a content-equality check before writing back to
  the db, so the same content never round-trips.

Debouncing:
  File save events from editors often fire 2–3 times in rapid succession
  (write, atime, sync). We debounce per-path with a small timer (default
  150ms) so we only act once per logical edit.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from watchdog.events import (
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from agent_core.state.db import Database
from agent_core.state.models import Obligation, ObligationStatus
from agent_core.state.renderer import _STATUS_TO_DIR  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

logger = logging.getLogger(__name__)


# ── Path classification ──────────────────────────────────────────────────────


_RENDERED_NAME = re.compile(r"-[0-9a-f]{8}\.md$")

# Reverse map: column dir name → ObligationStatus
_DIR_TO_STATUS = {v: k for k, v in _STATUS_TO_DIR.items()}


def is_rendered_obligation_path(path: Path, board_root: Path) -> bool:
    """True if ``path`` lives directly under one of the 4 OB column dirs and
    matches the renderer's filename pattern (`-<8hex>.md` suffix)."""
    try:
        rel = path.relative_to(board_root)
    except ValueError:
        return False
    parts = rel.parts
    if len(parts) != 2:
        return False
    column = parts[0]
    if column not in _DIR_TO_STATUS:
        return False
    return bool(_RENDERED_NAME.search(path.name))


def column_status(path: Path, board_root: Path) -> ObligationStatus | None:
    """Return the status implied by which column dir contains ``path``,
    or None if the path isn't under a column dir."""
    try:
        rel = path.relative_to(board_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 1:
        return None
    return _DIR_TO_STATUS.get(parts[0])


# ── Markdown parsing ─────────────────────────────────────────────────────────


@dataclass
class ParsedObligation:
    """The shape of a parsed obligation .md file."""

    id: str | None
    frontmatter: dict
    title: str
    body: str


def parse_obligation_md(text: str) -> ParsedObligation:
    """Parse a rendered obligation .md back into structured form.

    Tolerates files where the frontmatter block is missing or empty.
    """
    fm: dict = {}
    body_after_fm = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            raw_fm = text[4:end]
            try:
                fm = yaml.safe_load(raw_fm) or {}
            except yaml.YAMLError:
                logger.warning("could not parse frontmatter; treating as empty")
                fm = {}
            body_after_fm = text[end + 4 :].lstrip("\n")

    title = ""
    body_lines: list[str] = []
    for i, line in enumerate(body_after_fm.splitlines()):
        if i == 0 and line.startswith("# "):
            title = line[2:].strip()
            continue
        body_lines.append(line)

    body = "\n".join(body_lines).strip()
    return ParsedObligation(
        id=fm.get("id"),
        frontmatter=fm,
        title=title,
        body=body or None,
    )


# ── Db apply logic (the core; pure, testable without watchdog) ───────────────


@dataclass
class ApplyResult:
    """Outcome of applying a single file event."""

    action: str  # 'updated' | 'moved' | 'noop' | 'skipped' | 'not_found'
    obligation_id: str | None = None
    reason: str | None = None


def apply_modified(db: Database, path: Path, board_root: Path) -> ApplyResult:
    """Apply a 'modified' event: parse the file, update the matching db row.

    No-op if the file's content already reflects the db state (avoids
    feedback loops with the renderer).
    """
    if not is_rendered_obligation_path(path, board_root):
        return ApplyResult(action="skipped", reason="not a rendered OB file")

    if not path.exists():
        return ApplyResult(action="skipped", reason="file gone before parse")

    parsed = parse_obligation_md(path.read_text(encoding="utf-8"))
    if not parsed.id:
        return ApplyResult(action="skipped", reason="no id in frontmatter")

    expected_status = column_status(path, board_root)
    if expected_status is None:
        return ApplyResult(action="skipped", reason="not in a column dir")

    with db.session() as s:
        ob = s.get(Obligation, parsed.id)
        if ob is None:
            return ApplyResult(action="not_found", obligation_id=parsed.id)

        # Decide what (if anything) actually changed
        new_title = parsed.title or ob.title
        new_body = parsed.body
        # Status: prefer column position over frontmatter (column move is the
        # human-natural way to change status; frontmatter may lag)
        new_status = expected_status
        new_priority = parsed.frontmatter.get("priority", ob.priority)
        new_completion = parsed.frontmatter.get("completion_criteria", ob.completion_criteria)

        changed = False
        if new_title != ob.title:
            ob.title = new_title
            changed = True
        if new_body != ob.body:
            ob.body = new_body
            changed = True
        if new_status != ob.status:
            ob.status = new_status
            changed = True
        if new_priority != ob.priority:
            ob.priority = new_priority
            changed = True
        if new_completion != ob.completion_criteria:
            ob.completion_criteria = new_completion
            changed = True

        if not changed:
            return ApplyResult(action="noop", obligation_id=ob.id)

        ob.updated_at = datetime.utcnow()
        s.add(ob)
        s.commit()
        return ApplyResult(action="updated", obligation_id=ob.id)


def apply_moved(
    db: Database,
    src: Path,
    dest: Path,
    board_root: Path,
) -> ApplyResult:
    """Apply a 'moved' event between column dirs.

    Only acts if both source and destination are rendered obligation paths
    under different columns.
    """
    src_status = column_status(src, board_root)
    dest_status = column_status(dest, board_root)
    if src_status is None or dest_status is None:
        return ApplyResult(action="skipped", reason="move outside column dirs")
    if not _RENDERED_NAME.search(dest.name):
        return ApplyResult(action="skipped", reason="not a rendered OB file")
    if src_status == dest_status:
        return ApplyResult(action="noop", reason="same column")

    if not dest.exists():
        return ApplyResult(action="skipped", reason="dest file missing post-move")

    parsed = parse_obligation_md(dest.read_text(encoding="utf-8"))
    if not parsed.id:
        return ApplyResult(action="skipped", reason="no id in moved file")

    with db.session() as s:
        ob = s.get(Obligation, parsed.id)
        if ob is None:
            return ApplyResult(action="not_found", obligation_id=parsed.id)
        ob.status = dest_status
        ob.updated_at = datetime.utcnow()
        s.add(ob)
        s.commit()
        return ApplyResult(action="moved", obligation_id=ob.id)


# ── Watchdog event handler + observer ────────────────────────────────────────


class _DebouncedHandler(FileSystemEventHandler):
    """Watchdog handler that debounces per-path and dispatches to apply_*."""

    def __init__(self, db: Database, board_root: Path, debounce_ms: int = 150) -> None:
        self.db = db
        self.board_root = board_root
        self.debounce_seconds = debounce_ms / 1000.0
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        self._schedule(str(path), lambda: self._handle_modified(path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if not isinstance(event, FileMovedEvent):
            return
        src = Path(event.src_path)
        dest = Path(event.dest_path)
        # No debounce on move — moves are atomic single events
        self._handle_moved(src, dest)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # MVP: log only; never auto-delete from db
        logger.info(
            "OB file deleted on disk; db row left intact (delete via chat to remove): %s",
            event.src_path,
        )

    # ── Internals ──

    def _schedule(self, key: str, fn) -> None:  # type: ignore[no-untyped-def]
        """Cancel any prior pending timer for this key; schedule a new one."""
        with self._lock:
            existing = self._timers.pop(key, None)
            if existing is not None:
                existing.cancel()
            t = threading.Timer(self.debounce_seconds, fn)
            t.daemon = True
            self._timers[key] = t
            t.start()

    def _handle_modified(self, path: Path) -> None:
        try:
            result = apply_modified(self.db, path, self.board_root)
            if result.action == "updated":
                logger.info("watcher: updated obligation %s", result.obligation_id)
            elif result.action == "not_found":
                logger.warning(
                    "watcher: file references obligation_id %s not in db",
                    result.obligation_id,
                )
        except Exception:
            logger.exception("watcher: failed to apply modified for %s", path)

    def _handle_moved(self, src: Path, dest: Path) -> None:
        try:
            result = apply_moved(self.db, src, dest, self.board_root)
            if result.action == "moved":
                logger.info(
                    "watcher: moved obligation %s to %s",
                    result.obligation_id,
                    column_status(dest, self.board_root),
                )
        except Exception:
            logger.exception("watcher: failed to apply move %s → %s", src, dest)


class VaultWatcher:
    """Watches the ObligationBoard column dirs for human edits, syncs back to db.

    Usage:
        watcher = VaultWatcher(db, vault_path)
        watcher.start()
        # ... agent runs ...
        watcher.stop()
    """

    def __init__(
        self,
        db: Database,
        vault_path: Path | str,
        *,
        debounce_ms: int = 150,
    ) -> None:
        self.db = db
        self.vault_path = Path(vault_path)
        self.board_root = self.vault_path / "ObligationBoard"
        self._handler = _DebouncedHandler(db, self.board_root, debounce_ms=debounce_ms)
        self._observer: BaseObserver | None = None

    def start(self) -> None:
        if self._observer is not None:
            return
        self.board_root.mkdir(parents=True, exist_ok=True)
        # Make sure the column dirs exist so the watch is well-defined even
        # before the renderer ever ran.
        for col in _DIR_TO_STATUS:
            (self.board_root / col).mkdir(parents=True, exist_ok=True)

        obs = Observer()
        obs.schedule(self._handler, str(self.board_root), recursive=True)
        obs.start()
        self._observer = obs
        logger.info("vault watcher started on %s", self.board_root)

    def stop(self, timeout: float = 5.0) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=timeout)
        self._observer = None
        logger.info("vault watcher stopped")

    # Convenience for tests / one-shot reconciliation:
    def reconcile_once(self, paths: Iterable[Path]) -> list[ApplyResult]:
        """Apply each path as if a 'modified' event had fired. Useful for
        startup reconciliation after the watcher was offline."""
        results = []
        for p in paths:
            results.append(apply_modified(self.db, Path(p), self.board_root))
        return results


# Avoid noisy unused-import warning on time/threading (imported for handler).
_ = (time, threading)


__all__ = [
    "ApplyResult",
    "ParsedObligation",
    "VaultWatcher",
    "apply_modified",
    "apply_moved",
    "column_status",
    "is_rendered_obligation_path",
    "parse_obligation_md",
]
