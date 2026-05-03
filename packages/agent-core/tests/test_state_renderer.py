"""VaultRenderer tests.

Covers:
  - slugify edge cases
  - obligation filename stable + collision-resistant
  - obligation md serialization roundtrips structured fields
  - render_obligation_board lays out files in the correct column dirs
  - render is idempotent (no write on second invocation)
  - status change moves file to new column + deletes old
  - obligation deletion sweeps the rendered file
  - user-added files in column dirs are left alone (stale-sweep is conservative)
  - learning-rules renderer groups by tag, drops superseded, has 'general' first
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from agent_core.state import (
    Database,
    LearningRule,
    Obligation,
    ObligationStatus,
    VaultRenderer,
    obligation_filename,
    slugify,
)

# ── slugify ──────────────────────────────────────────────────────────────────


def test_slugify_basic() -> None:
    assert slugify("Reply to Charlotte's message") == "reply-to-charlotte-s-message"


def test_slugify_collapses_dashes() -> None:
    assert slugify("foo  ---  bar") == "foo-bar"


def test_slugify_truncates() -> None:
    long = "a" * 200
    assert len(slugify(long, max_len=20)) == 20


def test_slugify_empty_to_untitled() -> None:
    assert slugify("") == "untitled"
    assert slugify("!!!") == "untitled"


def test_slugify_unicode_replaces() -> None:
    # Non-ASCII chars get replaced — keeps things filesystem-safe.
    assert slugify("café — naïve") == "caf-na-ve"


# ── obligation filename ──────────────────────────────────────────────────────


def test_obligation_filename_format() -> None:
    ob = Obligation(
        title="Reply to test message",
        created_at=datetime(2026, 5, 2, 10, 30, tzinfo=UTC),
    )
    name = obligation_filename(ob)
    assert name.startswith("2026-05-02-reply-to-test-message-")
    assert name.endswith(".md")
    # 8-hex id suffix
    suffix = name[-11:-3]  # the 8-hex part before .md
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_obligation_filename_unique_per_id() -> None:
    a = Obligation(title="same title")
    b = Obligation(title="same title")
    assert obligation_filename(a) != obligation_filename(b)  # different ids


# ── ObligationBoard rendering ────────────────────────────────────────────────


def test_render_obligation_board_lays_out_columns(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(Obligation(title="In inbox", status=ObligationStatus.inbox))
        s.add(Obligation(title="Doing now", status=ObligationStatus.in_progress))
        s.add(Obligation(title="Blocked", status=ObligationStatus.waiting))
        s.add(Obligation(title="Finished", status=ObligationStatus.done))
        s.commit()

    r = VaultRenderer(db, tmp_path)
    result = r.render_obligation_board()

    assert (tmp_path / "ObligationBoard" / "inbox").is_dir()
    assert (tmp_path / "ObligationBoard" / "in-progress").is_dir()
    assert (tmp_path / "ObligationBoard" / "waiting").is_dir()
    assert (tmp_path / "ObligationBoard" / "done").is_dir()

    inbox_files = list((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))
    assert len(inbox_files) == 1
    assert "in-inbox" in inbox_files[0].name

    assert len(result.written) == 4
    assert len(result.unchanged) == 0
    assert len(result.deleted) == 0


def test_render_obligation_md_has_frontmatter_and_body(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(
            Obligation(
                title="Reply to charlotte",
                body="They asked about Q3 plans.",
                status=ObligationStatus.in_progress,
                priority=2,
                completion_criteria=[{"type": "email_sent", "to": "c@x.com"}],
            )
        )
        s.commit()

    VaultRenderer(db, tmp_path).render_obligation_board()

    files = list((tmp_path / "ObligationBoard" / "in-progress").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    assert text.startswith("---\n")
    # parse frontmatter
    fm_end = text.index("---", 4)
    fm = yaml.safe_load(text[4:fm_end])
    assert fm["status"] == "in-progress"
    assert fm["priority"] == 2
    assert fm["completion_criteria"] == [{"type": "email_sent", "to": "c@x.com"}]
    # body
    body_part = text[fm_end + 3 :]
    assert "# Reply to charlotte" in body_part
    assert "They asked about Q3 plans." in body_part


def test_render_obligation_board_idempotent(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(Obligation(title="x"))
        s.commit()

    r = VaultRenderer(db, tmp_path)
    first = r.render_obligation_board()
    assert len(first.written) == 1
    second = r.render_obligation_board()
    assert len(second.written) == 0
    assert len(second.unchanged) == 1


def test_render_obligation_board_status_change_moves_file(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        ob = Obligation(title="Move me", status=ObligationStatus.inbox)
        s.add(ob)
        s.commit()
        ob_id = ob.id

    r = VaultRenderer(db, tmp_path)
    r.render_obligation_board()
    assert list((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))

    # Status change → re-render
    with db.session() as s:
        ob = s.get(Obligation, ob_id)
        ob.status = ObligationStatus.done
        s.commit()

    result = r.render_obligation_board()
    # The old file should have been swept; the new file written
    assert len(list((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))) == 0
    assert len(list((tmp_path / "ObligationBoard" / "done").glob("*.md"))) == 1
    assert len(result.deleted) == 1
    assert len(result.written) == 1


def test_render_obligation_board_deletion_sweeps_file(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        ob = Obligation(title="Will be deleted")
        s.add(ob)
        s.commit()
        ob_id = ob.id

    r = VaultRenderer(db, tmp_path)
    r.render_obligation_board()
    assert list((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))

    with db.session() as s:
        ob = s.get(Obligation, ob_id)
        s.delete(ob)
        s.commit()

    result = r.render_obligation_board()
    assert len(list((tmp_path / "ObligationBoard" / "inbox").glob("*.md"))) == 0
    assert len(result.deleted) == 1


def test_render_obligation_board_leaves_user_files_alone(tmp_path: Path) -> None:
    """Files that don't match the rendered-obligation filename pattern should
    survive the stale-sweep — users may drop notes / attachments in column dirs."""
    db = Database.sqlite_memory()
    db.create_all()
    user_note = tmp_path / "ObligationBoard" / "inbox" / "my-personal-notes.md"
    user_note.parent.mkdir(parents=True, exist_ok=True)
    user_note.write_text("personal scribble")

    VaultRenderer(db, tmp_path).render_obligation_board()
    assert user_note.exists()
    assert user_note.read_text() == "personal scribble"


# ── Learning rules rendering ─────────────────────────────────────────────────


def test_render_learning_rules_groups_by_tag_general_first(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(LearningRule(correction="email rule A", skill_tags=["email-composer"]))
        s.add(LearningRule(correction="general rule B", skill_tags=["general"]))
        s.add(LearningRule(correction="doc rule C", skill_tags=["document-creator"]))
        s.commit()

    VaultRenderer(db, tmp_path).render_learning_rules()
    text = (tmp_path / "Learning-Rules.md").read_text()

    # General comes first
    pos_general = text.index("## general")
    pos_doc = text.index("## document-creator")
    pos_email = text.index("## email-composer")
    assert pos_general < pos_doc < pos_email
    # Each rule body present
    for body in ("email rule A", "general rule B", "doc rule C"):
        assert body in text


def test_render_learning_rules_drops_superseded(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        old = LearningRule(correction="OLD rule", skill_tags=["general"])
        s.add(old)
        s.commit()
        new = LearningRule(correction="NEW rule", skill_tags=["general"])
        s.add(new)
        s.commit()
        old.superseded_by = new.id
        s.commit()

    VaultRenderer(db, tmp_path).render_learning_rules()
    text = (tmp_path / "Learning-Rules.md").read_text()
    assert "OLD rule" not in text
    assert "NEW rule" in text


def test_render_learning_rules_empty_state(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    VaultRenderer(db, tmp_path).render_learning_rules()
    text = (tmp_path / "Learning-Rules.md").read_text()
    assert "No active learning rules yet" in text


def test_render_all_aggregates(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(Obligation(title="t"))
        s.add(LearningRule(correction="r"))
        s.commit()
    result = VaultRenderer(db, tmp_path).render_all()
    # 1 ob + 1 rules.md
    assert len(result.written) == 2


def test_render_all_idempotent_second_call(tmp_path: Path) -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(Obligation(title="t"))
        s.add(LearningRule(correction="r"))
        s.commit()
    r = VaultRenderer(db, tmp_path)
    r.render_all()
    second = r.render_all()
    assert len(second.written) == 0
    assert len(second.unchanged) == 2


# ── Created_at timestamp ──────────────────────────────────────────────────────


def test_obligation_filename_uses_created_date_not_now() -> None:
    """Filename uses the obligation's created_at date — not 'now' — so renders
    of historical obligations are stable."""
    past = datetime.now(UTC) - timedelta(days=30)
    ob = Obligation(title="historical", created_at=past)
    expected_date = past.date().isoformat()
    assert obligation_filename(ob).startswith(expected_date)
