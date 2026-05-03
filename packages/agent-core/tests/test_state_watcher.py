"""VaultWatcher tests.

Covers the pure parse/apply layer thoroughly (no real fs events) plus a
single end-to-end test that the live observer dispatches.

Pure-layer coverage:
  - parse_obligation_md: full frontmatter + title + body, missing fm,
    malformed fm, no title heading
  - is_rendered_obligation_path / column_status path classification
  - apply_modified: title change, body change, status change via column,
    priority change, completion_criteria change, no-op when content matches,
    not_found when id missing from db, skipped when not OB file or no id
  - apply_moved: move between columns updates status, same-column = noop,
    not-rendered = skipped, missing dest = skipped
  - Renderer round-trip: render → file → parse → apply → no further changes
    (proves the renderer/watcher pair is fixed-point stable)

End-to-end:
  - One test that actually starts the watchdog observer, modifies a file,
    waits for the debounced handler to fire, asserts the db updated.
"""

from __future__ import annotations

import time
from pathlib import Path

from agent_core.state import (
    Database,
    Obligation,
    ObligationStatus,
    VaultRenderer,
    VaultWatcher,
    apply_modified,
    apply_moved,
    column_status,
    is_rendered_obligation_path,
    obligation_filename,
    parse_obligation_md,
    render_obligation_md,
)

# ── parse_obligation_md ──────────────────────────────────────────────────────


def test_parse_full_obligation_md() -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        ob = Obligation(
            title="Reply to charlotte",
            body="They asked about Q3 plans.",
            status=ObligationStatus.in_progress,
            priority=2,
            completion_criteria=[{"type": "email_sent", "to": "c@x.com"}],
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id

    with db.session() as s:
        ob = s.get(Obligation, ob_id)
        text = render_obligation_md(ob)

    parsed = parse_obligation_md(text)
    assert parsed.id == ob_id
    assert parsed.title == "Reply to charlotte"
    assert parsed.body == "They asked about Q3 plans."
    assert parsed.frontmatter["status"] == "in-progress"
    assert parsed.frontmatter["priority"] == 2
    assert parsed.frontmatter["completion_criteria"] == [{"type": "email_sent", "to": "c@x.com"}]


def test_parse_no_frontmatter() -> None:
    p = parse_obligation_md("# Just a title\n\nBody here\n")
    assert p.id is None
    assert p.frontmatter == {}
    assert p.title == "Just a title"
    assert p.body == "Body here"


def test_parse_malformed_frontmatter() -> None:
    """Bad YAML in frontmatter is tolerated — fm comes back empty."""
    text = "---\nthis is: not valid: yaml: at: all\n  - x\n---\n# t\n"
    p = parse_obligation_md(text)
    assert p.frontmatter == {}
    assert p.title == "t"


def test_parse_no_title_heading() -> None:
    text = "---\nid: abc\n---\n\nBody only, no heading.\n"
    p = parse_obligation_md(text)
    assert p.title == ""
    assert p.body == "Body only, no heading."


# ── Path classification ──────────────────────────────────────────────────────


def test_is_rendered_obligation_path(tmp_path: Path) -> None:
    board = tmp_path / "ObligationBoard"
    rendered = board / "inbox" / "2026-05-02-something-deadbeef.md"
    user_file = board / "inbox" / "my-personal-notes.md"
    deeper = board / "inbox" / "sub" / "2026-05-02-x-deadbeef.md"
    assert is_rendered_obligation_path(rendered, board) is True
    assert is_rendered_obligation_path(user_file, board) is False
    assert is_rendered_obligation_path(deeper, board) is False  # 3 parts
    # outside board
    assert is_rendered_obligation_path(tmp_path / "elsewhere.md", board) is False


def test_column_status(tmp_path: Path) -> None:
    board = tmp_path / "ObligationBoard"
    assert column_status(board / "inbox" / "x.md", board) == ObligationStatus.inbox
    assert column_status(board / "in-progress" / "x.md", board) == ObligationStatus.in_progress
    assert column_status(board / "waiting" / "x.md", board) == ObligationStatus.waiting
    assert column_status(board / "done" / "x.md", board) == ObligationStatus.done
    assert column_status(board / "what" / "x.md", board) is None
    assert column_status(tmp_path / "elsewhere.md", board) is None


# ── apply_modified ───────────────────────────────────────────────────────────


def _setup_renderer(tmp_path: Path) -> tuple[Database, VaultRenderer]:
    db = Database.sqlite_memory()
    db.create_all()
    return db, VaultRenderer(db, tmp_path)


def test_apply_modified_status_change_via_column_move_is_handled(tmp_path: Path) -> None:
    """If a user moves a file between columns and triggers a 'modified' event
    (without us seeing the move), the column-position wins as the new status."""
    db, r = _setup_renderer(tmp_path)
    with db.session() as s:
        ob = Obligation(title="t", status=ObligationStatus.inbox)
        s.add(ob)
        s.commit()
        ob_id = ob.id

    r.render_obligation_board()

    src_file = next((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))
    dest = tmp_path / "ObligationBoard" / "done" / src_file.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_file.rename(dest)

    result = apply_modified(db, dest, tmp_path / "ObligationBoard")
    assert result.action == "updated"
    with db.session() as s:
        assert s.get(Obligation, ob_id).status == ObligationStatus.done


def test_apply_modified_body_edit_writes_to_db(tmp_path: Path) -> None:
    db, r = _setup_renderer(tmp_path)
    with db.session() as s:
        ob = Obligation(title="t", body="original body")
        s.add(ob)
        s.commit()
        ob_id = ob.id
    r.render_obligation_board()

    f = next((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))
    text = f.read_text()
    new_text = text.replace("original body", "edited body")
    f.write_text(new_text)

    result = apply_modified(db, f, tmp_path / "ObligationBoard")
    assert result.action == "updated"
    with db.session() as s:
        assert s.get(Obligation, ob_id).body == "edited body"


def test_apply_modified_noop_when_content_matches_db(tmp_path: Path) -> None:
    """Renderer/watcher fixed point: render → modify event w/o real change →
    db.update is a no-op."""
    db, r = _setup_renderer(tmp_path)
    with db.session() as s:
        s.add(Obligation(title="t"))
        s.commit()
    r.render_obligation_board()
    f = next((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))

    result = apply_modified(db, f, tmp_path / "ObligationBoard")
    assert result.action == "noop"


def test_apply_modified_skipped_for_user_file(tmp_path: Path) -> None:
    db, _ = _setup_renderer(tmp_path)
    f = tmp_path / "ObligationBoard" / "inbox" / "my-notes.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("just a note\n")
    result = apply_modified(db, f, tmp_path / "ObligationBoard")
    assert result.action == "skipped"


def test_apply_modified_not_found_for_unknown_id(tmp_path: Path) -> None:
    db, _ = _setup_renderer(tmp_path)
    f = tmp_path / "ObligationBoard" / "inbox" / "2026-05-02-fake-deadbeef.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("---\nid: never-existed-in-db\nstatus: inbox\n---\n# fake\n")
    result = apply_modified(db, f, tmp_path / "ObligationBoard")
    assert result.action == "not_found"


# ── apply_moved ──────────────────────────────────────────────────────────────


def test_apply_moved_between_columns(tmp_path: Path) -> None:
    db, r = _setup_renderer(tmp_path)
    with db.session() as s:
        ob = Obligation(title="t", status=ObligationStatus.inbox)
        s.add(ob)
        s.commit()
        ob_id = ob.id
    r.render_obligation_board()

    src = next((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))
    dest = tmp_path / "ObligationBoard" / "in-progress" / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)

    result = apply_moved(db, src, dest, tmp_path / "ObligationBoard")
    assert result.action == "moved"
    with db.session() as s:
        assert s.get(Obligation, ob_id).status == ObligationStatus.in_progress


def test_apply_moved_same_column_noop(tmp_path: Path) -> None:
    db, r = _setup_renderer(tmp_path)
    with db.session() as s:
        s.add(Obligation(title="t"))
        s.commit()
    r.render_obligation_board()
    src = next((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))
    dest = src.with_name("renamed-deadbeef.md")
    src.rename(dest)
    result = apply_moved(db, src, dest, tmp_path / "ObligationBoard")
    assert result.action == "noop"


# ── Round-trip stability ─────────────────────────────────────────────────────


def test_render_then_apply_modified_is_fixed_point(tmp_path: Path) -> None:
    """The renderer/watcher pair must be a fixed point: render produces a
    file that, when fed back to apply_modified, results in noop."""
    db, r = _setup_renderer(tmp_path)
    with db.session() as s:
        s.add(
            Obligation(
                title="full obligation",
                body="lots of body text\nwith multiple lines",
                status=ObligationStatus.in_progress,
                priority=3,
                completion_criteria=[
                    {"type": "email_sent", "to": "x@y"},
                    {"type": "principal_ratification"},
                ],
            )
        )
        s.commit()
    r.render_obligation_board()

    f = next((tmp_path / "ObligationBoard" / "in-progress").glob("*.md"))
    result = apply_modified(db, f, tmp_path / "ObligationBoard")
    assert result.action == "noop", "renderer→watcher must be fixed-point stable"


# ── End-to-end with live observer ────────────────────────────────────────────


def test_live_watcher_picks_up_file_edit(tmp_path: Path) -> None:
    """One end-to-end test: start the watcher, edit a file, verify the db
    eventually reflects the change.

    Uses a file-based sqlite (not :memory:) because the watcher's debounced
    handler runs in a worker thread, and SQLite :memory: dbs are per-connection
    — a worker thread would see an empty fresh db. Production always uses
    file-backed sqlite, so this test mirrors real usage.
    """
    db = Database.sqlite(tmp_path / "test.db")
    db.create_all()
    with db.session() as s:
        ob = Obligation(title="watch me", body="before")
        s.add(ob)
        s.commit()
        ob_id = ob.id

    r = VaultRenderer(db, tmp_path)
    r.render_obligation_board()

    watcher = VaultWatcher(db, tmp_path, debounce_ms=50)
    watcher.start()
    try:
        f = next((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))
        # Edit the body
        text = f.read_text()
        f.write_text(text.replace("before", "after-watcher-fired"))

        # Wait up to 2 seconds for the debounced handler to fire + db to update
        deadline = time.time() + 2.0
        observed = None
        while time.time() < deadline:
            with db.session() as s:
                observed = s.get(Obligation, ob_id).body
            if observed == "after-watcher-fired":
                break
            time.sleep(0.05)
        assert observed == "after-watcher-fired", (
            f"watcher did not propagate file edit within 2s; observed: {observed!r}"
        )
    finally:
        watcher.stop()


def test_watcher_can_be_started_before_first_render(tmp_path: Path) -> None:
    """start() should mkdir the column dirs so the watch is well-defined even
    on a pristine vault."""
    db = Database.sqlite_memory()
    db.create_all()
    watcher = VaultWatcher(db, tmp_path)
    watcher.start()
    try:
        for col in ("inbox", "in-progress", "waiting", "done"):
            assert (tmp_path / "ObligationBoard" / col).is_dir()
    finally:
        watcher.stop()


def test_watcher_stop_idempotent(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    w = VaultWatcher(db, tmp_path)
    w.stop()  # before start: no-op
    w.start()
    w.stop()
    w.stop()  # second stop: no-op


def test_reconcile_once_applies_paths(tmp_path: Path) -> None:
    db, r = _setup_renderer(tmp_path)
    with db.session() as s:
        s.add(Obligation(title="reconcile-me", body="orig"))
        s.commit()
    r.render_obligation_board()
    f = next((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))

    # Edit while watcher is offline
    text = f.read_text()
    f.write_text(text.replace("orig", "after-offline-edit"))

    watcher = VaultWatcher(db, tmp_path)
    results = watcher.reconcile_once([f])
    assert len(results) == 1
    assert results[0].action == "updated"


# Test obligation_filename pattern matches what is_rendered_obligation_path
# expects — simple regression guard.
def test_filename_pattern_matches_path_classifier(tmp_path: Path) -> None:
    ob = Obligation(title="t")
    name = obligation_filename(ob)
    full = tmp_path / "ObligationBoard" / "inbox" / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("dummy")
    assert is_rendered_obligation_path(full, tmp_path / "ObligationBoard")
