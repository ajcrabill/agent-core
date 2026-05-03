"""Sprint 5a — learning store + rule firings + correction candidates."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from agent_core.learning import (
    CorrectionCandidates,
    LearningStore,
    RuleFirings,
)
from agent_core.learning.store import jsonl_export
from agent_core.state import (
    CorrectionCandidateStatus,
    Database,
    RuleFiring,
    utcnow,
)


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


def _store(tmp_path: Path) -> LearningStore:
    return LearningStore(_empty_db(), jsonl_path=tmp_path / "learnings.jsonl")


# ── LearningStore: add ──────────────────────────────────────────────────────


def test_add_creates_rule_with_default_general_tag(tmp_path: Path) -> None:
    s = _store(tmp_path)
    r = s.add("Be concise.")
    assert r.skill_tags == ["general"]
    assert r.correction == "Be concise."
    assert r.superseded_by is None


def test_add_rejects_empty_skill_tags(tmp_path: Path) -> None:
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="at least one tag"):
        s.add("x", skill_tags=[])


def test_add_appends_to_jsonl(tmp_path: Path) -> None:
    jsonl = tmp_path / "learnings.jsonl"
    s = LearningStore(_empty_db(), jsonl_path=jsonl)
    r = s.add("rule one", skill_tags=["email"], source="test")
    assert jsonl.exists()
    lines = jsonl.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["id"] == r.id
    assert rec["correction"] == "rule one"
    assert rec["skill_tags"] == ["email"]


def test_add_skips_jsonl_when_disabled() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    s.add("x")
    # No jsonl_path attribute path created
    assert s.jsonl_path is None


# ── LearningStore: get / list / stats ───────────────────────────────────────


def test_get_by_id(tmp_path: Path) -> None:
    s = _store(tmp_path)
    r = s.add("y")
    fetched = s.get_by_id(r.id)
    assert fetched.id == r.id


def test_get_by_id_returns_none_for_missing(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.get_by_id("never-existed") is None


def test_get_by_tag_filters_to_tag(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add("e1", skill_tags=["email"])
    s.add("e2", skill_tags=["email", "general"])
    s.add("d1", skill_tags=["doc"])
    rows = s.get_by_tag("email")
    titles = [r.correction for r in rows]
    assert sorted(titles) == ["e1", "e2"]


def test_get_by_tag_recent_caps(tmp_path: Path) -> None:
    s = _store(tmp_path)
    for i in range(10):
        s.add(f"r{i}", skill_tags=["x"])
    assert len(s.get_by_tag("x", recent=3)) == 3


def test_get_by_tag_search_substring(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add("Use the Oxford comma", skill_tags=["writing"])
    s.add("Avoid passive voice", skill_tags=["writing"])
    rows = s.get_by_tag("writing", search="oxford")
    assert len(rows) == 1
    assert "Oxford" in rows[0].correction


def test_get_by_tag_excludes_superseded_by_default(tmp_path: Path) -> None:
    s = _store(tmp_path)
    old = s.add("old", skill_tags=["t"])
    s.supersede(old.id, "new")
    active = s.get_by_tag("t")
    assert [r.correction for r in active] == ["new"]
    assert {r.correction for r in s.get_by_tag("t", include_superseded=True)} == {"old", "new"}


def test_list_active_default_excludes_superseded(tmp_path: Path) -> None:
    s = _store(tmp_path)
    a = s.add("a")
    s.add("b")
    s.supersede(a.id, "a-new")
    active = [r.correction for r in s.list_active()]
    assert "a" not in active
    assert "a-new" in active and "b" in active


def test_stats_counts_correctly(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add("x", skill_tags=["general"])
    s.add("y", skill_tags=["email", "general"])
    old = s.add("z", skill_tags=["doc"])
    s.supersede(old.id, "z-new")  # adds new active under doc tag
    st = s.stats()
    assert st["active"] == 3  # x, y, z-new
    assert st["superseded"] == 1
    assert st["total"] == 4
    assert st["by_tag"]["general"] == 2
    assert st["by_tag"]["email"] == 1
    assert st["by_tag"]["doc"] == 1


# ── LearningStore: supersede + remove ───────────────────────────────────────


def test_supersede_marks_old_and_writes_new(tmp_path: Path) -> None:
    s = _store(tmp_path)
    old = s.add("old", skill_tags=["t"])
    new = s.supersede(old.id, "new")
    refreshed_old = s.get_by_id(old.id)
    assert refreshed_old.superseded_by == new.id
    assert new.skill_tags == ["t"]  # inherits


def test_supersede_unknown_id_raises(tmp_path: Path) -> None:
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        s.supersede("nope", "x")


def test_supersede_can_change_tags(tmp_path: Path) -> None:
    s = _store(tmp_path)
    old = s.add("x", skill_tags=["a"])
    new = s.supersede(old.id, "x2", skill_tags=["b", "c"])
    assert new.skill_tags == ["b", "c"]


def test_remove_hard_deletes(tmp_path: Path) -> None:
    s = _store(tmp_path)
    r = s.add("doomed")
    s.remove(r.id)
    assert s.get_by_id(r.id) is None


def test_remove_unknown_id_raises(tmp_path: Path) -> None:
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        s.remove("nope")


# ── JSONL import / export ───────────────────────────────────────────────────


def test_import_jsonl_round_trip(tmp_path: Path) -> None:
    src = _store(tmp_path)
    src.add("from-export-1", skill_tags=["t"])
    src.add("from-export-2", skill_tags=["t", "general"])

    export_path = tmp_path / "export.jsonl"
    count = jsonl_export(src, export_path)
    assert count == 2

    # Import into a fresh store
    dst = LearningStore(_empty_db(), write_ahead=False)
    imported = dst.import_jsonl(export_path)
    assert imported == 2
    titles = sorted(r.correction for r in dst.list_active())
    assert titles == ["from-export-1", "from-export-2"]


def test_import_jsonl_skips_existing_ids(tmp_path: Path) -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    s.add("already there")

    # Export it then re-import — should skip
    export_path = tmp_path / "e.jsonl"
    jsonl_export(s, export_path)
    imported = s.import_jsonl(export_path)
    assert imported == 0
    assert len(s.list_active()) == 1


def test_import_jsonl_normalizes_comma_separated_tags(tmp_path: Path) -> None:
    """Esby's older JSONL format has skill_tags as a comma-separated string."""
    src = tmp_path / "esby.jsonl"
    src.write_text(
        json.dumps(
            {
                "id": "abc-123",
                "correction": "Use 'their' not 'his/her'",
                "skill_tags": "writing,general",  # comma-separated
                "source": "esby legacy",
            }
        )
        + "\n"
    )
    s = LearningStore(_empty_db(), write_ahead=False)
    s.import_jsonl(src)
    rule = s.get_by_id("abc-123")
    assert rule.skill_tags == ["writing", "general"]


def test_import_jsonl_skips_tombstones(tmp_path: Path) -> None:
    src = tmp_path / "with-tombstone.jsonl"
    src.write_text(
        json.dumps({"_tombstone": "deleted-id"})
        + "\n"
        + json.dumps({"id": "live", "correction": "real one", "skill_tags": ["t"]})
        + "\n"
    )
    s = LearningStore(_empty_db(), write_ahead=False)
    n = s.import_jsonl(src)
    assert n == 1


# ── RuleFirings ─────────────────────────────────────────────────────────────


def test_record_firing_persists() -> None:
    db = _empty_db()
    store = LearningStore(db, write_ahead=False)
    rule = store.add("x")
    fire = RuleFirings(db).record(rule_id=rule.id, skill="email-triage")
    assert fire.rule_id == rule.id
    assert fire.skill == "email-triage"


def test_count_for_rule() -> None:
    db = _empty_db()
    store = LearningStore(db, write_ahead=False)
    r = store.add("x")
    f = RuleFirings(db)
    f.record(rule_id=r.id)
    f.record(rule_id=r.id)
    f.record(rule_id=r.id, was_overridden=True)
    assert f.count_for_rule(r.id) == 3
    assert f.overrides_for_rule(r.id) == 1


def test_for_rule_orders_newest_first() -> None:
    db = _empty_db()
    store = LearningStore(db, write_ahead=False)
    r = store.add("x")
    f = RuleFirings(db)
    f.record(rule_id=r.id, skill="a")
    f.record(rule_id=r.id, skill="b")
    rows = f.for_rule(r.id)
    # The latest should be "b"
    assert rows[0].skill == "b"


def test_stale_rules_finds_never_fired() -> None:
    db = _empty_db()
    store = LearningStore(db, write_ahead=False)
    store.add("never used")
    stale = RuleFirings(db).stale_rules(days=90)
    assert len(stale) == 1


def test_stale_rules_finds_not_fired_in_window() -> None:
    db = _empty_db()
    store = LearningStore(db, write_ahead=False)
    rule = store.add("aged out")
    # Manually backdate a firing
    f = RuleFirings(db)
    fired = f.record(rule_id=rule.id)
    with db.session() as s:
        from sqlmodel import select

        row = s.exec(select(RuleFiring).where(RuleFiring.id == fired.id)).first()
        row.fired_at = utcnow() - timedelta(days=120)
        s.add(row)
        s.commit()

    stale = RuleFirings(db).stale_rules(days=90)
    assert any(r.id == rule.id for r in stale)


def test_recently_fired_rules_not_stale() -> None:
    db = _empty_db()
    store = LearningStore(db, write_ahead=False)
    rule = store.add("hot")
    RuleFirings(db).record(rule_id=rule.id)
    stale = RuleFirings(db).stale_rules(days=30)
    assert not any(r.id == rule.id for r in stale)


# ── CorrectionCandidates ────────────────────────────────────────────────────


def test_propose_creates_pending_candidate() -> None:
    cc = CorrectionCandidates(_empty_db()).propose(
        detected_correction="Use 'their' not 'his/her'",
        confidence=0.85,
        source_excerpt="actually use their",
    )
    assert cc.status == CorrectionCandidateStatus.pending
    assert cc.confidence == 0.85
    assert cc.inferred_skill_tags == ["general"]


def test_pending_lists_only_pending() -> None:
    db = _empty_db()
    cc_api = CorrectionCandidates(db)
    a = cc_api.propose(detected_correction="A")
    b = cc_api.propose(detected_correction="B")
    cc_api.reject(a.id)
    pending = cc_api.pending()
    assert len(pending) == 1
    assert pending[0].id == b.id


def test_promote_creates_learning_rule_and_marks_candidate() -> None:
    db = _empty_db()
    store = LearningStore(db, write_ahead=False)
    cc_api = CorrectionCandidates(db, store=store)
    cand = cc_api.propose(
        detected_correction="Be brief",
        inferred_skill_tags=["email"],
        source_session="sess-1",
    )
    rule = cc_api.promote(cand.id)

    assert rule.correction == "Be brief"
    assert rule.skill_tags == ["email"]
    assert "correction-candidate" in rule.source

    # Candidate is marked promoted + linked to rule
    refreshed = cc_api.get(cand.id)
    assert refreshed.status == CorrectionCandidateStatus.promoted
    assert refreshed.promoted_to_rule_id == rule.id


def test_promote_can_edit_correction_text() -> None:
    db = _empty_db()
    cc_api = CorrectionCandidates(db)
    cand = cc_api.propose(detected_correction="rough draft of rule")
    rule = cc_api.promote(cand.id, edited_correction="polished version")
    assert rule.correction == "polished version"


def test_promote_can_override_skill_tags() -> None:
    db = _empty_db()
    cc_api = CorrectionCandidates(db)
    cand = cc_api.propose(
        detected_correction="x",
        inferred_skill_tags=["wrong-tag"],
    )
    rule = cc_api.promote(cand.id, skill_tags=["correct-tag"])
    assert rule.skill_tags == ["correct-tag"]


def test_reject_marks_rejected() -> None:
    db = _empty_db()
    cc_api = CorrectionCandidates(db)
    cand = cc_api.propose(detected_correction="x")
    rejected = cc_api.reject(cand.id)
    assert rejected.status == CorrectionCandidateStatus.rejected


def test_expire_marks_expired() -> None:
    db = _empty_db()
    cc_api = CorrectionCandidates(db)
    cand = cc_api.propose(detected_correction="x")
    expired = cc_api.expire(cand.id)
    assert expired.status == CorrectionCandidateStatus.expired


def test_promote_fails_for_already_promoted() -> None:
    db = _empty_db()
    cc_api = CorrectionCandidates(db)
    cand = cc_api.propose(detected_correction="x")
    cc_api.promote(cand.id)
    with pytest.raises(ValueError, match="already promoted"):
        cc_api.promote(cand.id)


def test_promote_unknown_id_raises() -> None:
    cc_api = CorrectionCandidates(_empty_db())
    with pytest.raises(ValueError, match="not found"):
        cc_api.promote("nope")
