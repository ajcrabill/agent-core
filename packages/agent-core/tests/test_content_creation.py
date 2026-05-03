"""Sprint 5c — content-creation primitives + L21 synthetic battery."""

from __future__ import annotations

from datetime import timedelta

import pytest
from agent_core.content_creation import (
    CalibrationManager,
    DiffExtractor,
    ExemplarStore,
    Iterations,
    ProposedRule,
    SyntheticBattery,
)
from agent_core.state import (
    Database,
    Exemplar,
    IterationStatus,
    utcnow,
)


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


# ── ExemplarStore ────────────────────────────────────────────────────────────


def test_exemplar_add_default_not_synthetic() -> None:
    es = ExemplarStore(_empty_db())
    ex = es.add(skill="email-composer", content="Hi …")
    assert ex.is_synthetic is False
    assert ex.skill == "email-composer"


def test_exemplar_add_synthetic_flag_persists() -> None:
    es = ExemplarStore(_empty_db())
    ex = es.add(skill="s", content="x", is_synthetic=True)
    assert ex.is_synthetic is True
    assert es.count_synthetic("s") == 1
    assert es.count_natural("s") == 0


def test_exemplar_count_natural_vs_synthetic() -> None:
    es = ExemplarStore(_empty_db())
    es.add(skill="s", content="a")
    es.add(skill="s", content="b")
    es.add(skill="s", content="c", is_synthetic=True)
    assert es.count_natural("s") == 2
    assert es.count_synthetic("s") == 1
    assert es.count_total("s") == 3


def test_exemplar_get_by_skill_excludes_synthetic_when_asked() -> None:
    es = ExemplarStore(_empty_db())
    es.add(skill="s", content="real")
    es.add(skill="s", content="generated", is_synthetic=True)
    naturals = es.get_by_skill("s", include_synthetic=False)
    assert len(naturals) == 1
    assert naturals[0].content == "real"


def test_exemplar_remove_unknown_raises() -> None:
    es = ExemplarStore(_empty_db())
    with pytest.raises(ValueError, match="not found"):
        es.remove("nope")


# ── CalibrationManager ──────────────────────────────────────────────────────


def test_calibration_get_creates_default_row() -> None:
    cal = CalibrationManager(_empty_db())
    row = cal.get("email-composer")
    assert row.skill == "email-composer"
    assert row.autonomous_mode is False
    assert row.confidence == 0.0
    assert row.consecutive_ratifications == 0


def test_calibration_record_attempt_increments_counts() -> None:
    cal = CalibrationManager(_empty_db())
    row = cal.record_attempt("s", ratified=True, confidence=0.9)
    assert row.attempts_count == 1
    assert row.ratifications_count == 1
    assert row.consecutive_ratifications == 1
    assert row.confidence == 0.9


def test_calibration_promotes_to_autonomous_after_threshold_met() -> None:
    """5 consecutive ratifications + confidence ≥ 0.85 → autonomous_mode=True."""
    cal = CalibrationManager(_empty_db(), default_threshold=0.85, ratifications_required=5)
    for _ in range(4):
        cal.record_attempt("s", ratified=True, confidence=0.9)
    assert cal.is_autonomous("s") is False  # only 4 in a row
    cal.record_attempt("s", ratified=True, confidence=0.9)
    assert cal.is_autonomous("s") is True


def test_calibration_does_not_promote_without_confidence() -> None:
    cal = CalibrationManager(_empty_db(), default_threshold=0.85, ratifications_required=3)
    for _ in range(5):
        cal.record_attempt("s", ratified=True, confidence=0.5)  # below threshold
    assert cal.is_autonomous("s") is False


def test_calibration_failure_resets_streak_and_demotes() -> None:
    cal = CalibrationManager(_empty_db(), default_threshold=0.85, ratifications_required=3)
    for _ in range(5):
        cal.record_attempt("s", ratified=True, confidence=0.9)
    assert cal.is_autonomous("s") is True
    cal.record_attempt("s", ratified=False)
    assert cal.is_autonomous("s") is False
    row = cal.get("s")
    assert row.consecutive_ratifications == 0


def test_calibration_reset_clears_state() -> None:
    cal = CalibrationManager(_empty_db(), default_threshold=0.85, ratifications_required=3)
    for _ in range(5):
        cal.record_attempt("s", ratified=True, confidence=0.95)
    cal.reset("s")
    row = cal.get("s")
    assert row.confidence == 0.0
    assert row.consecutive_ratifications == 0
    assert row.autonomous_mode is False


# ── Iterations ───────────────────────────────────────────────────────────────


def test_iteration_start_creates_in_progress() -> None:
    iters = Iterations(_empty_db())
    it = iters.start(skill="s", raw_input="raw text")
    assert it.status == IterationStatus.in_progress
    assert it.is_synthetic is False
    assert it.attempts == []


def test_iteration_add_attempt_appends() -> None:
    iters = Iterations(_empty_db())
    it = iters.start(skill="s", raw_input="r")
    iters.add_attempt(it.id, content="first", model="qwen")
    iters.add_attempt(it.id, content="second", model="qwen")
    refreshed = iters.get(it.id)
    assert len(refreshed.attempts) == 2
    assert refreshed.attempts[0]["n"] == 0
    assert refreshed.attempts[1]["content"] == "second"


def test_iteration_add_correction_appends() -> None:
    iters = Iterations(_empty_db())
    it = iters.start(skill="s", raw_input="r")
    iters.add_attempt(it.id, content="first")
    iters.add_correction(it.id, narrative="too long", diff=None)
    refreshed = iters.get(it.id)
    assert len(refreshed.corrections) == 1
    assert refreshed.corrections[0]["narrative"] == "too long"


def test_iteration_ratify_promotes_last_attempt_to_exemplar() -> None:
    iters = Iterations(_empty_db())
    it = iters.start(skill="email-composer", raw_input="r")
    iters.add_attempt(it.id, content="draft v1")
    iters.add_attempt(it.id, content="draft v2 — final")
    ex = iters.ratify(it.id, exemplar_title="BD outreach #1")
    assert ex.skill == "email-composer"
    assert ex.content == "draft v2 — final"
    assert ex.title == "BD outreach #1"
    assert ex.source_iteration_id == it.id
    refreshed = iters.get(it.id)
    assert refreshed.status == IterationStatus.ratified
    assert refreshed.final_content == "draft v2 — final"


def test_iteration_ratify_synthetic_carries_flag_to_exemplar() -> None:
    iters = Iterations(_empty_db())
    it = iters.start(skill="s", raw_input="r", is_synthetic=True)
    iters.add_attempt(it.id, content="x")
    ex = iters.ratify(it.id)
    assert ex.is_synthetic is True


def test_iteration_ratify_explicit_final_content_used() -> None:
    iters = Iterations(_empty_db())
    it = iters.start(skill="s", raw_input="r")
    iters.add_attempt(it.id, content="draft")
    ex = iters.ratify(it.id, final_content="hand-edited final")
    assert ex.content == "hand-edited final"


def test_iteration_ratify_requires_attempt_or_explicit_final() -> None:
    iters = Iterations(_empty_db())
    it = iters.start(skill="s", raw_input="r")
    with pytest.raises(ValueError, match="no final_content"):
        iters.ratify(it.id)


def test_iteration_ratify_updates_calibration() -> None:
    db = _empty_db()
    iters = Iterations(db)
    it = iters.start(skill="email", raw_input="r")
    iters.add_attempt(it.id, content="x")
    iters.ratify(it.id, confidence=0.9)
    cal = CalibrationManager(db).get("email")
    assert cal.attempts_count == 1
    assert cal.ratifications_count == 1
    assert cal.confidence == 0.9


def test_iteration_abandon_marks_abandoned_and_resets_streak() -> None:
    db = _empty_db()
    iters = Iterations(db)
    it = iters.start(skill="email", raw_input="r")
    iters.add_attempt(it.id, content="x")
    abandoned = iters.abandon(it.id, reason="not worth it")
    assert abandoned.status == IterationStatus.abandoned
    cal = CalibrationManager(db).get("email")
    assert cal.consecutive_ratifications == 0


def test_iteration_cant_add_attempt_after_ratify() -> None:
    iters = Iterations(_empty_db())
    it = iters.start(skill="s", raw_input="r")
    iters.add_attempt(it.id, content="x")
    iters.ratify(it.id)
    with pytest.raises(ValueError, match="cannot add attempt"):
        iters.add_attempt(it.id, content="y")


def test_iterations_in_progress_filter() -> None:
    iters = Iterations(_empty_db())
    a = iters.start(skill="s1", raw_input="r")
    iters.start(skill="s2", raw_input="r")
    iters.add_attempt(a.id, content="x")
    iters.ratify(a.id)
    assert {i.skill for i in iters.in_progress()} == {"s2"}
    assert {i.skill for i in iters.in_progress(skill="s2")} == {"s2"}


# ── DiffExtractor protocol satisfaction ─────────────────────────────────────


class _StubExtractor:
    def extract(self, *, original, corrected, narrative=None, skill=None):
        return ProposedRule(
            correction="be more concise",
            skill_tags=[skill or "general"],
            confidence=0.8,
            rationale="user removed filler words",
        )


def test_diff_extractor_protocol_satisfaction() -> None:
    e = _StubExtractor()
    assert isinstance(e, DiffExtractor)


def test_diff_extractor_returns_proposed_rule() -> None:
    e = _StubExtractor()
    r = e.extract(original="…", corrected="…", skill="email")
    assert r is not None
    assert r.skill_tags == ["email"]
    assert 0.0 <= r.confidence <= 1.0


# ── SyntheticBattery (L21) ──────────────────────────────────────────────────


class _StubGenerator:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    def generate(self, *, skill, exemplars, recent_corrections, count):
        self.calls += 1
        return self.outputs[:count]


def test_battery_eligibility_fails_without_enough_natural_exemplars() -> None:
    bat = SyntheticBattery(
        _empty_db(), min_natural_exemplars=15, min_days_of_data=0, min_correction_themes=0
    )
    elig = bat.check_eligibility("s")
    assert elig.eligible is False
    assert "natural exemplars" in elig.reason


def test_battery_eligibility_fails_without_enough_days() -> None:
    db = _empty_db()
    es = ExemplarStore(db)
    for i in range(20):
        es.add(skill="s", content=f"e{i}")
    # All exemplars created NOW → days_of_data = 0
    bat = SyntheticBattery(
        db,
        min_natural_exemplars=15,
        min_days_of_data=7,
        min_correction_themes=0,
    )
    elig = bat.check_eligibility("s")
    assert elig.eligible is False
    assert "natural data" in elig.reason


def test_battery_eligibility_fails_without_correction_diversity() -> None:
    db = _empty_db()
    es = ExemplarStore(db)
    # 15 exemplars, all backdated 10 days
    for i in range(15):
        ex = es.add(skill="s", content=f"e{i}")
        with db.session() as s:
            row = s.get(Exemplar, ex.id)
            row.created_at = utcnow() - timedelta(days=10)
            s.add(row)
            s.commit()
    bat = SyntheticBattery(
        db, min_natural_exemplars=15, min_days_of_data=7, min_correction_themes=3
    )
    elig = bat.check_eligibility("s")
    assert elig.eligible is False
    assert "correction themes" in elig.reason


def test_battery_eligibility_passes_when_all_criteria_met() -> None:
    db = _empty_db()
    es = ExemplarStore(db)
    iters = Iterations(db, exemplar_store=es)

    # 15 natural exemplars, backdated 10 days
    for i in range(15):
        ex = es.add(skill="s", content=f"e{i}")
        with db.session() as s:
            row = s.get(Exemplar, ex.id)
            row.created_at = utcnow() - timedelta(days=10)
            s.add(row)
            s.commit()

    # 3 distinct correction themes via 3 iterations
    for theme in ("tone", "length", "structure"):
        it = iters.start(skill="s", raw_input="r")
        iters.add_correction(it.id, narrative=f"{theme} needs work")

    bat = SyntheticBattery(
        db, min_natural_exemplars=15, min_days_of_data=7, min_correction_themes=3
    )
    elig = bat.check_eligibility("s")
    assert elig.eligible is True
    assert elig.distinct_correction_themes >= 3


def test_battery_generate_batch_creates_synthetic_iterations() -> None:
    """End-to-end: eligible skill → generator runs → N synthetic iterations
    appear, marked is_synthetic=True."""
    db = _empty_db()
    es = ExemplarStore(db)
    iters = Iterations(db, exemplar_store=es)

    for i in range(15):
        ex = es.add(skill="s", content=f"e{i}")
        with db.session() as s:
            row = s.get(Exemplar, ex.id)
            row.created_at = utcnow() - timedelta(days=10)
            s.add(row)
            s.commit()
    for theme in ("tone", "length", "structure"):
        it = iters.start(skill="s", raw_input="r")
        iters.add_correction(it.id, narrative=f"{theme} needs work")

    bat = SyntheticBattery(
        db,
        exemplar_store=es,
        iterations=iters,
        min_natural_exemplars=15,
        min_days_of_data=7,
        min_correction_themes=3,
    )
    gen = _StubGenerator(["edge case 1", "edge case 2", "edge case 3"])
    new_iters = bat.generate_batch(skill="s", count=3, generator=gen)
    assert len(new_iters) == 3
    for it in new_iters:
        assert it.is_synthetic is True
        assert it.status == IterationStatus.in_progress

    # Recent_corrections fed to the generator (3 from above)
    assert gen.calls == 1


def test_battery_generate_batch_raises_when_not_eligible() -> None:
    bat = SyntheticBattery(_empty_db(), min_natural_exemplars=15)
    with pytest.raises(ValueError, match="not eligible"):
        bat.generate_batch(skill="s", count=3, generator=_StubGenerator(["x"]))


def test_battery_audit_overfit_compares_rates() -> None:
    """If synthetic ratifications outpace natural, delta > 0."""
    db = _empty_db()
    iters = Iterations(db)
    # 1 natural that's NOT ratified
    nat = iters.start(skill="s", raw_input="r")
    iters.add_attempt(nat.id, content="x")
    iters.abandon(nat.id, reason="too hard")
    # 2 synthetic, both ratified
    for _ in range(2):
        syn = iters.start(skill="s", raw_input="r", is_synthetic=True)
        iters.add_attempt(syn.id, content="x")
        iters.ratify(syn.id)

    bat = SyntheticBattery(db)
    overfit = bat.audit_overfit("s")
    assert overfit["natural_ratification_rate"] == 0.0
    assert overfit["synthetic_ratification_rate"] == 1.0
    assert overfit["delta"] == 1.0


def test_battery_eligibility_summary_renders() -> None:
    bat = SyntheticBattery(_empty_db(), min_natural_exemplars=15)
    elig = bat.check_eligibility("s")
    s = elig.as_summary()
    assert "not eligible" in s


# ── Generator protocol satisfaction ─────────────────────────────────────────


def test_battery_generator_protocol_satisfaction() -> None:
    from agent_core.content_creation import BatteryGenerator

    gen = _StubGenerator(["x"])
    assert isinstance(gen, BatteryGenerator)
