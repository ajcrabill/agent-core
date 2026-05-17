"""Tests for agent_core.testing — the harness itself.

These verify the AgentTestBed builder + stubs do what they advertise.
The E2E tests in test_e2e.py are the proof-of-use; this file is the unit
tests for the harness."""

from __future__ import annotations

import pytest
from agent_core.openbrain.embeddings import (
    OllamaEmbeddingProvider,
    SemanticStubProvider,
    StubEmbeddingProvider,
)
from agent_core.settings import AgentSettings
from agent_core.state import Database
from agent_core.testing import (
    AgentTestBed,
    StubAuditorModel,
    StubDiffExtractor,
    StubPlanDeveloper,
    StubStepExecutor,
)

# ── AgentTestBed: defaults ────────────────────────────────────────────────


def test_create_yields_balanced_preset_by_default() -> None:
    bed = AgentTestBed.create()
    assert bed.settings.autonomy.default_policy == "balanced"


def test_create_uses_in_memory_sqlite_by_default() -> None:
    bed = AgentTestBed.create()
    assert isinstance(bed.db, Database)
    # Schema is created — querying any table should not raise
    from agent_core.state.models import Obligation
    from sqlmodel import select

    with bed.db.session() as s:
        list(s.exec(select(Obligation)).all())


def test_create_swaps_ollama_for_stub_to_avoid_network() -> None:
    """Default settings have embedding_provider=ollama, but the test bed
    should swap it for the stub so tests don't hit the network."""
    bed = AgentTestBed.create()
    assert isinstance(bed.openbrain.embeddings, StubEmbeddingProvider)


# ── AgentTestBed: customization ───────────────────────────────────────────


def test_create_with_named_preset() -> None:
    bed = AgentTestBed.create(preset="cautious")
    assert bed.settings.autonomy.default_policy == "cautious"
    assert bed.settings.learning.detector_strictness == "loose"


def test_create_with_dict_overrides() -> None:
    bed = AgentTestBed.create(settings={"learning": {"detector_strictness": "strict"}})
    assert bed.settings.learning.detector_strictness == "strict"


def test_create_with_explicit_settings_object_honors_ollama_choice() -> None:
    """If the caller hands us an AgentSettings explicitly, we don't second-
    guess their embedding_provider choice."""
    s = AgentSettings(openbrain={"embedding_provider": "ollama"})  # type: ignore[arg-type]
    bed = AgentTestBed.create(settings=s)
    assert isinstance(bed.openbrain.embeddings, OllamaEmbeddingProvider)


def test_create_with_dict_can_explicitly_pick_stub_semantic() -> None:
    bed = AgentTestBed.create(settings={"openbrain": {"embedding_provider": "stub-semantic"}})
    assert isinstance(bed.openbrain.embeddings, SemanticStubProvider)


def test_create_with_supplied_db() -> None:
    db = Database.sqlite_memory()
    db.create_all()
    bed = AgentTestBed.create(db=db)
    assert bed.db is db


# ── AgentTestBed: lazy components + cache invalidation ────────────────────


def test_components_are_lazy() -> None:
    bed = AgentTestBed.create()
    assert bed._openbrain is None
    _ = bed.openbrain  # noqa
    assert bed._openbrain is not None


def test_with_setting_invalidates_cached_components() -> None:
    bed = AgentTestBed.create()
    first_dispatcher = bed.dispatcher
    bed.with_setting("notifications.enabled", True).with_setting("notifications.ntfy_topic", "x")
    assert bed.dispatcher is not first_dispatcher
    assert bed.dispatcher.enabled is True


def test_with_setting_returns_self_for_chaining() -> None:
    bed = AgentTestBed.create()
    result = bed.with_setting("learning.detector_strictness", "strict")
    assert result is bed


def test_with_setting_rejects_deep_paths() -> None:
    bed = AgentTestBed.create()
    with pytest.raises(ValueError):
        bed.with_setting("autonomy.per_action_overrides.send_email_external", "autonomous")


# ── Stubs ─────────────────────────────────────────────────────────────────


def test_stub_plan_developer_returns_configured_steps() -> None:
    custom = [{"description": "do thing", "action_class": "read"}]
    dev = StubPlanDeveloper(steps=custom, confidence=0.7)
    proposal = dev.develop(obligation=None, context=None)  # type: ignore[arg-type]
    assert proposal.steps == custom
    assert proposal.confidence == 0.7
    assert dev.calls == 1


def test_stub_plan_developer_default_steps_are_a_2step_plan() -> None:
    dev = StubPlanDeveloper()
    proposal = dev.develop(obligation=None, context=None)  # type: ignore[arg-type]
    assert len(proposal.steps) == 2


def test_stub_step_executor_records_calls() -> None:
    from types import SimpleNamespace

    ex = StubStepExecutor()
    plan = SimpleNamespace(id="plan-abc")
    ex.execute(plan=plan, step_index=0, context=None)  # type: ignore[arg-type]
    ex.execute(plan=plan, step_index=1, context=None)  # type: ignore[arg-type]
    assert ex.calls == [("plan-abc", 0), ("plan-abc", 1)]


def test_stub_auditor_model_returns_configured_score() -> None:
    aud = StubAuditorModel(score=0.42, passed=False)
    score = aud.audit(task_type="x", subject_model="m", output_summary="hi")
    assert score.score == pytest.approx(0.42)
    assert score.passed is False
    assert len(aud.calls) == 1


def test_stub_diff_extractor_default_returns_proposed_rule() -> None:
    ex = StubDiffExtractor()
    rule = ex.extract(original="A", corrected="B")
    assert rule is not None
    assert rule.confidence == pytest.approx(0.85)
    assert ex.calls[0]["original"] == "A"


def test_stub_diff_extractor_can_return_none() -> None:
    ex = StubDiffExtractor(returns=None)
    assert ex.extract(original="A", corrected="B") is None
