"""End-to-end tests — golden-path scenarios that exercise full pipelines.

These tests intentionally cross module boundaries. Per-module tests cover
component correctness; these cover the *flow* — the kind of thing a real
install does dozens of times a day.

If a scenario here breaks, it usually means a refactor changed an API in a
way the per-module tests didn't catch. Treat them as integration smoke tests.

Use ``AgentTestBed`` from ``agent_core.testing`` — it constructs a fully-wired
agent with one line so tests focus on behavior, not setup."""

from __future__ import annotations

import pytest

from agent_core.notifications import Notification, Urgency
from agent_core.state.models import (
    CorrectionCandidateStatus,
    LearningRule,
    ObligationSource,
    ObligationStatus,
)
from agent_core.testing import (
    AgentTestBed,
    StubAuditorModel,
    scenarios,
)


# ── Inbound → Obligation ───────────────────────────────────────────────────


def test_e2e_chat_inbound_lands_in_inbox() -> None:
    """A chat message becomes an Obligation with the right defaults."""
    bed = AgentTestBed.create()
    ob = scenarios.receive_chat(bed, text="Please review the Q3 budget")
    assert ob.status == ObligationStatus.inbox
    assert ob.source == ObligationSource.principal_chat
    assert "Q3 budget" in ob.title or "Q3 budget" in (ob.body or "")
    # Default completion criterion is principal_ratification per inbound module
    assert any(c.get("type") == "principal_ratification" for c in ob.completion_criteria)


def test_e2e_email_inbound_lands_in_inbox() -> None:
    bed = AgentTestBed.create()
    ob = scenarios.receive_email(
        bed,
        sender="charlotte@example.com",
        subject="Budget gap discussion",
        body="Following up on our Tuesday call.",
    )
    assert ob.status == ObligationStatus.inbox
    assert ob.source == ObligationSource.inbound_email


# ── OpenBrain capture → recall ─────────────────────────────────────────────


def test_e2e_capture_then_recall_returns_relevant_context() -> None:
    """A skill captures something today; a different skill recalls it tomorrow."""
    bed = AgentTestBed.create(settings={"openbrain": {"embedding_provider": "stub-semantic"}})

    scenarios.remember(
        bed,
        content="Q3 board meeting: discussed the budget gap and the rightsizing plan.",
        source_kind="vault",
        source_uri="vault/Q3-board-mtg.md",
    )
    scenarios.remember(
        bed,
        content="Charlotte mentioned the Q3 budget gap on a call last Tuesday.",
        source_kind="gmail",
    )
    scenarios.remember(bed, content="Random unrelated thought about lunch.")

    hits = scenarios.recall(bed, query="budget gap", limit=2)
    assert len(hits) == 2
    contents = [h.thought.content.lower() for h in hits]
    assert any("budget gap" in c for c in contents)
    assert not any("lunch" in c for c in contents)


def test_e2e_recall_carries_source_provenance() -> None:
    """Search results include the source_kind/uri so the agent can cite."""
    bed = AgentTestBed.create(settings={"openbrain": {"embedding_provider": "stub-semantic"}})
    scenarios.remember(
        bed,
        content="The CFO raised the rightsizing question first.",
        source_kind="vault",
        source_uri="vault/cfo-meeting.md",
    )
    hits = scenarios.recall(bed, query="rightsizing")
    assert len(hits) >= 1
    sources = hits[0].sources
    assert len(sources) >= 1
    assert sources[0].source_kind == "vault"
    assert sources[0].source_uri == "vault/cfo-meeting.md"


# ── Supervised learning loop ───────────────────────────────────────────────


def test_e2e_supervised_correction_to_promoted_rule() -> None:
    """Full loop: detector catches a correction → candidate persists →
    user promotes → LearningRule exists and is queryable."""
    bed = AgentTestBed.create()

    # User message containing a clear correction
    cand = scenarios.capture_correction(
        bed,
        text="Please use 'percentage delta', not 'absolute dollars'",
        skill="reports",
    )
    assert cand is not None
    assert cand.status == CorrectionCandidateStatus.pending
    assert cand.confidence > 0

    rule = scenarios.promote_candidate(bed, cand.id)
    assert isinstance(rule, LearningRule)

    # The candidate is now marked promoted and points at the new rule
    refetched = bed.candidates.get(cand.id)
    assert refetched is not None
    assert refetched.status == CorrectionCandidateStatus.promoted
    assert refetched.promoted_to_rule_id == rule.id

    # And the rule is in the active set returned by the store
    active = bed.learning_store.list_active()
    assert any(r.id == rule.id for r in active)


def test_e2e_detector_silent_on_neutral_chat() -> None:
    """Neutral chat doesn't generate spurious candidates."""
    bed = AgentTestBed.create()
    cand = scenarios.capture_correction(
        bed,
        text="Thanks, that looks good.",
    )
    assert cand is None
    assert bed.candidates.pending() == []


def test_e2e_detector_strictness_setting_changes_recall() -> None:
    """Loose detector catches things balanced/strict do not.

    Marginal phrasing ('actually') should fire under loose but be filtered
    out under strict (which raises the confidence floor)."""
    # Loose strictness drops confidence floor; strict raises it. We use a
    # marginal phrase whose pattern confidence sits between the two thresholds.
    bed_loose = AgentTestBed.create(
        settings={"learning": {"detector_min_confidence": 0.5}}
    )
    bed_strict = AgentTestBed.create(
        settings={"learning": {"detector_min_confidence": 0.7}}
    )

    marginal = "Actually, please send via email."
    loose_cand = scenarios.capture_correction(bed_loose, text=marginal)
    strict_cand = scenarios.capture_correction(bed_strict, text=marginal)

    assert loose_cand is not None  # 0.65 confidence ≥ 0.5
    assert strict_cand is None  # 0.65 confidence < 0.7


# ── Quality / agentic feedback ─────────────────────────────────────────────


def test_e2e_audit_passes_when_score_above_threshold() -> None:
    """A high-scoring auditor → audit row passed=True."""
    bed = AgentTestBed.create()
    auditor = StubAuditorModel(score=0.95, passed=True)
    audit = scenarios.audit_skill_run(
        bed,
        skill="email-triage",
        task_id="task-1",
        output="Triaged 12 messages, all archive.",
        auditor_model=auditor,
    )
    assert audit.passed is True
    assert audit.score == pytest.approx(0.95)
    assert len(auditor.calls) == 1


def test_e2e_audit_fails_when_score_below_threshold() -> None:
    bed = AgentTestBed.create()  # default pass_threshold=0.8 (balanced quality)
    auditor = StubAuditorModel(score=0.4)
    audit = scenarios.audit_skill_run(
        bed,
        skill="email-triage",
        task_id="task-2",
        output="Mishandled flagging.",
        auditor_model=auditor,
    )
    assert audit.passed is False


# ── Notifications gating ───────────────────────────────────────────────────


def test_e2e_default_install_doesnt_push() -> None:
    """A freshly-defaulted agent silently drops every notification.

    Regression test for the "quiet by default" preference."""
    bed = AgentTestBed.create()
    result = bed.dispatcher.notify(
        Notification(title="hello", body="world", urgency=Urgency.critical)
    )
    assert result.dropped
    assert result.reason == "disabled"


def test_e2e_with_setting_rebuilds_dispatcher() -> None:
    """Flipping a setting via with_setting() must invalidate cached components."""
    bed = AgentTestBed.create()
    # Initially disabled
    assert bed.dispatcher.enabled is False

    bed.with_setting("notifications.enabled", True).with_setting(
        "notifications.ntfy_topic", "test-topic-7x9"
    )
    # New dispatcher should reflect the new settings
    assert bed.dispatcher.enabled is True


# ── Settings propagation across components ─────────────────────────────────


def test_e2e_openbrain_search_default_limit_honored() -> None:
    """search() with no limit= uses settings.openbrain.search_default_limit."""
    bed = AgentTestBed.create(
        settings={"openbrain": {"search_default_limit": 2}}
    )
    for i in range(10):
        bed.openbrain.capture(f"thought number {i}")
    hits = bed.openbrain.search("query")
    assert len(hits) == 2


def test_e2e_pipeline_thresholds_from_settings() -> None:
    """PipelineMonitor picks up custom thresholds from settings."""
    bed = AgentTestBed.create(
        settings={"work": {"pipeline_in_progress_threshold_hours": 4}}
    )
    assert bed.pipeline_monitor.in_progress_threshold_hours == 4


# ── Backup → restore round-trip ────────────────────────────────────────────


def test_e2e_backup_then_restore_into_fresh_bed(tmp_path) -> None:
    """Take a snapshot of one agent's state, restore it into another, verify
    the new agent sees the data."""
    from agent_core.ops import create_backup, read_backup, restore_backup, write_backup
    from agent_core.state import Database

    src = AgentTestBed.create()
    scenarios.receive_chat(src, text="Important Q3 task")
    scenarios.remember(src, content="The Q3 board met on Tuesday.")

    backup_path = tmp_path / "snapshot.json"
    write_backup(create_backup(src.db), backup_path)

    # Fresh target
    target_db = Database.sqlite_memory()
    target_db.create_all()
    payload = read_backup(backup_path)
    restore_backup(target_db, payload, confirm=True, skip_schema_check=True)

    target = AgentTestBed(src.settings, target_db)
    # Obligation survived
    from sqlmodel import select

    from agent_core.state.models import Obligation, Thought

    with target.db.session() as s:
        obs = list(s.exec(select(Obligation)).all())
        thoughts = list(s.exec(select(Thought)).all())
    assert len(obs) == 1
    assert "Q3" in obs[0].body
    assert len(thoughts) == 1
    assert "Tuesday" in thoughts[0].content
