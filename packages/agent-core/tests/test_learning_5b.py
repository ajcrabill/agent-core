"""Sprint 5b — supervised-learning UX layer:
- HeuristicDetector
- MaintenanceScan (duplicates / conflicts / stale / compactable)
- WeeklyLearningReviewBuilder
- Seed-pack loader
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from agent_core.learning import (
    CorrectionCandidates,
    HeuristicDetector,
    LearningStore,
    MaintenanceScan,
    RuleFirings,
    WeeklyLearningReviewBuilder,
    list_packs,
    load_pack,
    pack_metadata,
)
from agent_core.state import (
    Database,
    LearningRule,
    utcnow,
)


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


# ── HeuristicDetector ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "msg",
    [
        "Don't include Cindy in any ESB-related comms.",
        "Stop using 'Hi all' — use 'Team' instead.",
        "Use percentage delta, not absolute dollars, when describing financial impact.",
        "From now on, schedule lunch slots at 12:30 not noon.",
        "Actually, use the legal name not the nickname for board minutes.",
        "Remember to CC AJ on every batch draft.",
        "Next time, lead with the recommendation, not the analysis.",
    ],
)
def test_heuristic_detector_catches_explicit_corrections(msg: str) -> None:
    d = HeuristicDetector()
    result = d.detect(principal_message=msg)
    assert result is not None
    assert result.confidence >= 0.6
    assert result.correction_text


@pytest.mark.parametrize(
    "msg",
    [
        "Hey thanks for the update.",
        "Sure, let's do that.",
        "How's the day going?",
        "I'll be at the meeting.",
        "Charlotte sounds great.",
        "",
    ],
)
def test_heuristic_detector_skips_neutral_chat(msg: str) -> None:
    d = HeuristicDetector()
    assert d.detect(principal_message=msg) is None


def test_detector_uses_skill_in_context_for_tag() -> None:
    d = HeuristicDetector()
    result = d.detect(
        principal_message="Don't include footer text in cold emails.",
        skill_in_context="email-composer",
    )
    assert result is not None
    assert result.inferred_skill_tags == ["email-composer"]


def test_detector_defaults_to_general_tag() -> None:
    result = HeuristicDetector().detect(principal_message="From now on, be brief.")
    assert result is not None
    assert result.inferred_skill_tags == ["general"]


def test_detector_truncates_long_excerpts() -> None:
    long_msg = "Don't ever do " + "the thing " * 100
    d = HeuristicDetector(max_excerpt_chars=80)
    result = d.detect(principal_message=long_msg)
    assert result is not None
    assert len(result.source_excerpt) <= 80
    assert len(result.correction_text) <= 80


def test_detector_min_confidence_filters() -> None:
    """A weak match shouldn't surface if min_confidence is high."""
    d = HeuristicDetector(min_confidence=0.95)
    # 'actually,' is a 0.65 confidence pattern; below 0.95 → no detection
    assert d.detect(principal_message="actually, that works") is None


def test_detector_satisfies_protocol() -> None:
    """HeuristicDetector should be runtime-checkable as a CorrectionDetector."""
    from agent_core.learning import CorrectionDetector

    d = HeuristicDetector()
    assert isinstance(d, CorrectionDetector)


# ── End-to-end: detector → CorrectionCandidates ─────────────────────────────


def test_detector_output_flows_into_candidate_proposal() -> None:
    db = _empty_db()
    detector = HeuristicDetector()
    cc_api = CorrectionCandidates(db)

    detected = detector.detect(
        principal_message="Don't use 'Hi all' anymore — use 'Team' instead.",
        skill_in_context="email-composer",
    )
    assert detected is not None

    cand = cc_api.propose(
        detected_correction=detected.correction_text,
        inferred_skill_tags=detected.inferred_skill_tags,
        source_excerpt=detected.source_excerpt,
        confidence=detected.confidence,
    )
    assert cand.confidence == detected.confidence
    assert cand.inferred_skill_tags == ["email-composer"]


# ── MaintenanceScan: duplicates ─────────────────────────────────────────────


def test_maintenance_finds_duplicates() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    s.add("Use Oxford commas always", skill_tags=["writing"])
    s.add("Always use Oxford commas", skill_tags=["writing"])  # ~duplicate
    s.add("Avoid passive voice", skill_tags=["writing"])  # different
    report = MaintenanceScan(db).run()
    assert len(report.duplicates) >= 1
    pair = report.duplicates[0]
    assert "oxford" in pair.rule_a_text.lower()
    assert "oxford" in pair.rule_b_text.lower()


def test_maintenance_duplicates_threshold_respected() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    s.add("Use Oxford commas", skill_tags=["writing"])
    s.add("Avoid passive voice", skill_tags=["writing"])
    # very high threshold → no duplicates
    report = MaintenanceScan(db, duplicate_threshold=0.99).run()
    assert report.duplicates == []


# ── MaintenanceScan: conflicts ──────────────────────────────────────────────


def test_maintenance_finds_conflicts() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    s.add("Use first names in greetings", skill_tags=["email"])
    s.add("Use formal salutations always", skill_tags=["email"])
    report = MaintenanceScan(db).run()
    # Both start with 'use' on the email tag, but content is different
    assert len(report.conflicts) >= 1
    c = report.conflicts[0]
    assert c.shared_tag == "email"
    assert c.leading_verb == "use"


# ── MaintenanceScan: stale ──────────────────────────────────────────────────


def test_maintenance_includes_stale_rules() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    s.add("never fired rule")
    report = MaintenanceScan(db, stale_days=30).run()
    # Never-fired rules count as stale
    assert len(report.stale) == 1


# ── MaintenanceScan: compactable ────────────────────────────────────────────


def test_maintenance_finds_compactable_clusters() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    for i in range(6):
        s.add(f"BD email rule #{i}", skill_tags=["bd-email"])
    report = MaintenanceScan(db, compactable_min_cluster=5).run()
    assert len(report.compactable) == 1
    cluster = report.compactable[0]
    assert cluster.tag == "bd-email"
    assert len(cluster.rule_ids) == 6


def test_maintenance_skips_general_for_compactable() -> None:
    """The 'general' tag is too broad to flag as compactable — most installs
    will accumulate many distinct general rules."""
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    for i in range(20):
        s.add(f"general rule {i}", skill_tags=["general"])
    report = MaintenanceScan(db).run()
    assert all(c.tag != "general" for c in report.compactable)


def test_maintenance_clean_report() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    s.add("solo rule")
    # add a firing so it's not stale
    rule = s.list_active()[0]
    RuleFirings(db).record(rule_id=rule.id)
    report = MaintenanceScan(db).run()
    md = report.as_markdown()
    assert report.rules_scanned == 1
    assert "No findings" in md or not report.has_findings()


def test_maintenance_markdown_renders_findings() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    s.add("Use Oxford commas")
    s.add("Always use Oxford commas")
    md = MaintenanceScan(db).run().as_markdown()
    assert "Possible duplicates" in md


# ── WeeklyLearningReview ────────────────────────────────────────────────────


def test_weekly_review_empty_when_steady() -> None:
    review = WeeklyLearningReviewBuilder(_empty_db()).build()
    assert not review.has_anything_to_review()
    assert "Nothing to review" in review.as_markdown()


def test_weekly_review_lists_pending_candidates() -> None:
    db = _empty_db()
    cc_api = CorrectionCandidates(db)
    cc_api.propose(detected_correction="don't do X", confidence=0.85)
    cc_api.propose(detected_correction="use Y instead", confidence=0.7)
    review = WeeklyLearningReviewBuilder(db).build()
    assert len(review.pending_candidates) == 2


def test_weekly_review_lists_promoted_in_window() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    s.add("just promoted")
    review = WeeklyLearningReviewBuilder(db).build()
    assert len(review.promoted_this_week) == 1


def test_weekly_review_excludes_old_promotions() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    rule = s.add("ancient rule")
    # Backdate
    from sqlmodel import select

    with db.session() as ses:
        r = ses.exec(select(LearningRule).where(LearningRule.id == rule.id)).first()
        r.created_at = utcnow() - timedelta(days=30)
        ses.add(r)
        ses.commit()
    review = WeeklyLearningReviewBuilder(db).build()
    assert review.promoted_this_week == []


def test_weekly_review_top_firing_rules() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    a = s.add("rule a")
    b = s.add("rule b")
    f = RuleFirings(db)
    for _ in range(5):
        f.record(rule_id=a.id)
    f.record(rule_id=b.id)
    review = WeeklyLearningReviewBuilder(db).build()
    assert len(review.top_firing_rules) == 2
    assert review.top_firing_rules[0]["firings"] == 5
    assert review.top_firing_rules[0]["id"] == a.id[:8]


def test_weekly_review_markdown_renders_all_sections() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    cc_api = CorrectionCandidates(db, store=s)
    s.add("Use Oxford commas")
    s.add("Always use Oxford commas")  # duplicate
    cc_api.propose(detected_correction="be brief", confidence=0.9)
    md = WeeklyLearningReviewBuilder(db).build().as_markdown()
    assert "Pending correction candidates" in md
    assert "Rules promoted this week" in md
    assert "Possible duplicates" in md


# ── Seed packs ──────────────────────────────────────────────────────────────


def test_list_packs_includes_professional() -> None:
    packs = list_packs()
    assert "professional" in packs


def test_pack_metadata_describes_pack() -> None:
    md = pack_metadata("professional")
    assert "Professional" in md["name"]
    assert md["rule_count"] > 0


def test_load_pack_inserts_rules() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    added = load_pack("professional", s)
    assert len(added) > 0
    # Every rule should have the pre-seed source marker
    for rule in added:
        assert rule.source.startswith("pre-seed:professional")


def test_load_pack_from_arbitrary_path(tmp_path: Path) -> None:
    pack_path = tmp_path / "custom.yaml"
    pack_path.write_text(
        "name: Custom\n"
        "description: test\n"
        "rules:\n"
        "  - correction: Custom rule one\n"
        "    skill_tags: [custom]\n"
        "  - correction: Custom rule two\n"
    )
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    added = load_pack(str(pack_path), s)
    assert len(added) == 2
    assert added[0].correction == "Custom rule one"
    assert added[0].skill_tags == ["custom"]
    assert added[1].skill_tags == ["general"]  # default


def test_load_pack_unknown_name_raises() -> None:
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    with pytest.raises(FileNotFoundError):
        load_pack("nope-does-not-exist", s)


def test_load_pack_skips_invalid_entries(tmp_path: Path) -> None:
    pack_path = tmp_path / "messy.yaml"
    pack_path.write_text(
        "rules:\n"
        "  - correction: valid one\n"
        "  - {}\n"  # missing correction
        "  - notes: orphan\n"  # no correction
    )
    db = _empty_db()
    added = load_pack(str(pack_path), LearningStore(db, write_ahead=False))
    assert len(added) == 1


def test_load_pack_custom_source_marker(tmp_path: Path) -> None:
    pack_path = tmp_path / "p.yaml"
    pack_path.write_text("rules:\n  - correction: r1\n")
    db = _empty_db()
    s = LearningStore(db, write_ahead=False)
    added = load_pack(str(pack_path), s, source_marker="custom-test-marker")
    assert added[0].source == "custom-test-marker"


def test_load_pack_rules_must_be_list(tmp_path: Path) -> None:
    pack_path = tmp_path / "bad.yaml"
    pack_path.write_text("rules: not-a-list\n")
    db = _empty_db()
    with pytest.raises(ValueError, match="must be a list"):
        load_pack(str(pack_path), LearningStore(db, write_ahead=False))
