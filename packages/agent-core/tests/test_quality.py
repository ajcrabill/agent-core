"""Sprint 4 quality-auditor tests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from agent_core.quality import (
    AuditScore,
    QualityAuditor,
    SamplingPolicy,
    generate_weekly_report,
)
from agent_core.state import (
    Database,
    QualityAudit,
    QualityScore,
    utcnow,
)
from sqlmodel import select


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


# ── Stub auditor models ──────────────────────────────────────────────────────


class FixedScoreAuditor:
    """Always returns the same score. Useful for deterministic tests."""

    def __init__(self, score: float, passed: bool | None = None, notes: str = "") -> None:
        self.score = score
        # If `passed` isn't explicit, derive from score >= 0.6
        self.passed = passed if passed is not None else score >= 0.6
        self.notes = notes
        self.calls = 0

    def audit(self, *, task_type, subject_model, output_summary, rubrics=None):
        self.calls += 1
        return AuditScore(score=self.score, passed=self.passed, primary_notes=self.notes)


class ScriptedAuditor:
    """Returns scores from a queue, in order."""

    def __init__(self, scores: list[float]) -> None:
        self.scores = list(scores)
        self.idx = 0

    def audit(self, *, task_type, subject_model, output_summary, rubrics=None):
        if self.idx >= len(self.scores):
            raise RuntimeError("ScriptedAuditor ran out of scores")
        s = self.scores[self.idx]
        self.idx += 1
        return AuditScore(score=s, passed=s >= 0.6)


# ── Audit basics ─────────────────────────────────────────────────────────────


def test_audit_writes_quality_audit_row() -> None:
    db = _empty_db()
    qa = QualityAuditor(db, primary_auditor=FixedScoreAuditor(0.8))
    audit = qa.audit(
        task_type="email-triage",
        task_id="task-1",
        subject_model="qwen3.5",
        output_summary="classified as P1 reply-needed",
    )
    assert audit.audit_level == 1
    assert audit.subject_model == "qwen3.5"
    assert audit.task_type == "email-triage"
    assert audit.task_id == "task-1"
    assert audit.score == 0.8
    assert audit.passed is True


def test_audit_uses_orchestrator_threshold_for_passed() -> None:
    """The orchestrator's threshold is authoritative; the model's `passed`
    bool is captured but not used for the persisted `passed` column."""
    db = _empty_db()
    # Model says passed=True but score is below threshold
    auditor = FixedScoreAuditor(0.5, passed=True)
    qa = QualityAuditor(db, primary_auditor=auditor, pass_threshold=0.7)
    audit = qa.audit(
        task_type="email-triage",
        task_id="t",
        subject_model="m",
        output_summary="x",
    )
    assert audit.passed is False  # 0.5 < 0.7


def test_audit_creates_score_row_on_first_audit() -> None:
    db = _empty_db()
    QualityAuditor(db, primary_auditor=FixedScoreAuditor(0.8)).audit(
        task_type="email-triage", task_id="t", subject_model="m", output_summary="x"
    )
    with db.session() as s:
        row = s.exec(select(QualityScore)).first()
    assert row.subject_model == "m"
    assert row.task_type == "email-triage"
    assert row.total_audited == 1
    assert row.running_avg == 0.8


def test_audit_updates_running_stats() -> None:
    db = _empty_db()
    qa = QualityAuditor(db, primary_auditor=ScriptedAuditor([0.8, 0.6, 1.0]))
    for i in range(3):
        qa.audit(task_type="t", task_id=f"t-{i}", subject_model="m", output_summary="x")
    with db.session() as s:
        row = s.exec(select(QualityScore)).first()
    assert row.total_audited == 3
    assert pytest.approx(row.running_avg, abs=0.001) == (0.8 + 0.6 + 1.0) / 3
    assert pytest.approx(row.running_sum, abs=0.001) == 0.8 + 0.6 + 1.0


def test_last_n_avg_uses_window() -> None:
    db = _empty_db()
    qa = QualityAuditor(
        db,
        primary_auditor=ScriptedAuditor([1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]),
        last_n_window=3,
    )
    for i in range(8):
        qa.audit(task_type="t", task_id=f"t-{i}", subject_model="m", output_summary="x")
    with db.session() as s:
        row = s.exec(select(QualityScore)).first()
    # Last 3 audits scored 0.0, 0.0, 0.0
    assert row.last_n_avg == pytest.approx(0.0)
    # Overall stays balanced
    assert pytest.approx(row.running_avg, abs=0.001) == 5 / 8


# ── Auto-undelegation ───────────────────────────────────────────────────────


def test_undelegation_after_n_consecutive_failures() -> None:
    db = _empty_db()
    qa = QualityAuditor(
        db,
        primary_auditor=ScriptedAuditor([0.5, 0.5, 0.5]),
        undelegation_strikes=3,
        pass_threshold=0.6,
    )
    assert qa.is_delegated(subject_model="m", task_type="t") is True
    qa.audit(task_type="t", task_id="1", subject_model="m", output_summary="x")
    assert qa.is_delegated(subject_model="m", task_type="t") is True  # 1 strike
    qa.audit(task_type="t", task_id="2", subject_model="m", output_summary="x")
    assert qa.is_delegated(subject_model="m", task_type="t") is True  # 2 strikes
    qa.audit(task_type="t", task_id="3", subject_model="m", output_summary="x")
    assert qa.is_delegated(subject_model="m", task_type="t") is False  # 3 strikes


def test_pass_resets_strikes() -> None:
    db = _empty_db()
    qa = QualityAuditor(
        db,
        primary_auditor=ScriptedAuditor([0.5, 0.5, 0.9, 0.5]),
        undelegation_strikes=3,
    )
    for i in range(4):
        qa.audit(task_type="t", task_id=f"x{i}", subject_model="m", output_summary="x")
    # Two failures then a pass (resets) then a failure → only 1 strike
    assert qa.is_delegated(subject_model="m", task_type="t") is True


def test_restore_delegation_resets_state() -> None:
    db = _empty_db()
    qa = QualityAuditor(
        db,
        primary_auditor=FixedScoreAuditor(0.0),
        undelegation_strikes=2,
    )
    qa.audit(task_type="t", task_id="1", subject_model="m", output_summary="x")
    qa.audit(task_type="t", task_id="2", subject_model="m", output_summary="x")
    assert qa.is_delegated(subject_model="m", task_type="t") is False
    qa.restore_delegation(subject_model="m", task_type="t")
    assert qa.is_delegated(subject_model="m", task_type="t") is True
    with db.session() as s:
        row = s.exec(select(QualityScore)).first()
    assert row.strikes == 0


def test_list_undelegated() -> None:
    db = _empty_db()
    qa = QualityAuditor(
        db,
        primary_auditor=FixedScoreAuditor(0.0),
        undelegation_strikes=2,
    )
    qa.audit(task_type="t1", task_id="a", subject_model="m1", output_summary="x")
    qa.audit(task_type="t1", task_id="b", subject_model="m1", output_summary="x")
    assert any(s.subject_model == "m1" and s.task_type == "t1" for s in qa.list_undelegated())


def test_is_delegated_defaults_true_for_unseen_combo() -> None:
    qa = QualityAuditor(_empty_db(), primary_auditor=FixedScoreAuditor(1.0))
    assert qa.is_delegated(subject_model="never-seen", task_type="never-tested") is True


# ── Meta-auditor (level 2) ──────────────────────────────────────────────────


def test_meta_auditor_runs_every_n() -> None:
    db = _empty_db()
    primary = FixedScoreAuditor(0.8)
    meta = FixedScoreAuditor(0.95)
    qa = QualityAuditor(
        db,
        primary_auditor=primary,
        meta_auditor=meta,
        meta_audit_every_n=3,
    )
    for i in range(7):
        qa.audit(task_type="t", task_id=f"x{i}", subject_model="m", output_summary="x")
    # After 3 + 6 primaries → 2 meta-audits should have fired
    with db.session() as s:
        meta_rows = list(s.exec(select(QualityAudit).where(QualityAudit.audit_level == 2)).all())
    assert len(meta_rows) == 2
    assert meta.calls == 2


def test_no_meta_auditor_means_no_level_2_rows() -> None:
    db = _empty_db()
    qa = QualityAuditor(db, primary_auditor=FixedScoreAuditor(0.8))
    for i in range(20):
        qa.audit(task_type="t", task_id=f"x{i}", subject_model="m", output_summary="x")
    with db.session() as s:
        meta = list(s.exec(select(QualityAudit).where(QualityAudit.audit_level == 2)).all())
    assert meta == []


# ── Sampling policy ──────────────────────────────────────────────────────────


def test_sampling_low_confidence_always_audits() -> None:
    p = SamplingPolicy(_empty_db(), low_confidence_threshold=0.7)
    yes, reason = p.should_audit(task_type="t", subject_model="m", confidence=0.5)
    assert yes is True
    assert reason == "low_confidence"


def test_sampling_bootstrap_always_audits_first_n() -> None:
    db = _empty_db()
    qa = QualityAuditor(db, primary_auditor=FixedScoreAuditor(0.8))
    p = SamplingPolicy(db, base_rate=0.0, bootstrap_count=5, rng=lambda: 0.99)
    # First 5 should be audited regardless of rng
    for i in range(5):
        yes, reason = p.should_audit(task_type="t", subject_model="m", confidence=1.0)
        assert yes is True
        assert reason == "bootstrap"
        # Persist an audit so the bootstrap counter advances
        qa.audit(task_type="t", task_id=f"x{i}", subject_model="m", output_summary="x")
    # 6th should be skipped (base_rate=0, rng=0.99 > 0)
    yes, reason = p.should_audit(task_type="t", subject_model="m", confidence=1.0)
    assert yes is False
    assert reason == "skipped_random"


def test_sampling_random_rate_with_deterministic_rng() -> None:
    db = _empty_db()
    qa = QualityAuditor(db, primary_auditor=FixedScoreAuditor(0.8))
    # Bootstrap past
    for i in range(10):
        qa.audit(task_type="t", task_id=f"x{i}", subject_model="m", output_summary="x")
    p_yes = SamplingPolicy(db, base_rate=0.5, bootstrap_count=5, rng=lambda: 0.4)
    p_no = SamplingPolicy(db, base_rate=0.5, bootstrap_count=5, rng=lambda: 0.6)
    assert p_yes.should_audit(task_type="t", subject_model="m", confidence=1.0)[0] is True
    assert p_no.should_audit(task_type="t", subject_model="m", confidence=1.0)[0] is False


def test_sampling_invalid_base_rate_raises() -> None:
    with pytest.raises(ValueError):
        SamplingPolicy(_empty_db(), base_rate=1.5)


# ── Weekly report ────────────────────────────────────────────────────────────


def test_weekly_report_aggregates_by_model_task_type() -> None:
    db = _empty_db()
    qa = QualityAuditor(db, primary_auditor=ScriptedAuditor([0.8, 0.4, 0.9, 0.5]))
    qa.audit(task_type="t1", task_id="a", subject_model="m1", output_summary="x")
    qa.audit(task_type="t1", task_id="b", subject_model="m1", output_summary="x")
    qa.audit(task_type="t2", task_id="c", subject_model="m1", output_summary="x")
    qa.audit(task_type="t1", task_id="d", subject_model="m2", output_summary="x")

    report = generate_weekly_report(db)
    by_combo = {(s.subject_model, s.task_type): s for s in report.by_combo}
    assert ("m1", "t1") in by_combo
    assert ("m1", "t2") in by_combo
    assert ("m2", "t1") in by_combo
    assert by_combo[("m1", "t1")].audits_in_window == 2
    assert by_combo[("m1", "t1")].pass_count == 1  # 0.8 passed, 0.4 failed
    assert report.total_audits == 4


def test_weekly_report_excludes_audits_outside_window() -> None:
    db = _empty_db()
    qa = QualityAuditor(db, primary_auditor=FixedScoreAuditor(0.8))
    qa.audit(task_type="t", task_id="a", subject_model="m", output_summary="x")

    # Backdate the audit to 30 days ago
    with db.session() as s:
        row = s.exec(select(QualityAudit)).first()
        row.audited_at = utcnow() - timedelta(days=30)
        s.add(row)
        s.commit()

    report = generate_weekly_report(db, period_days=7)
    assert report.total_audits == 0


def test_weekly_report_lists_undelegated() -> None:
    db = _empty_db()
    qa = QualityAuditor(
        db,
        primary_auditor=FixedScoreAuditor(0.0),
        undelegation_strikes=2,
    )
    qa.audit(task_type="t", task_id="a", subject_model="m", output_summary="x")
    qa.audit(task_type="t", task_id="b", subject_model="m", output_summary="x")

    report = generate_weekly_report(db)
    assert len(report.currently_undelegated) == 1
    assert report.currently_undelegated[0].subject_model == "m"


def test_weekly_report_markdown_renders() -> None:
    db = _empty_db()
    qa = QualityAuditor(db, primary_auditor=FixedScoreAuditor(0.8))
    qa.audit(task_type="t", task_id="a", subject_model="m", output_summary="x")
    md = generate_weekly_report(db).as_markdown()
    assert "Quality audit" in md
    assert "Per-(model, task_type) breakdown" in md
    assert "`m`" in md
    assert "`t`" in md


def test_weekly_report_empty_state_renders() -> None:
    md = generate_weekly_report(_empty_db()).as_markdown()
    assert "No audits in this period" in md
