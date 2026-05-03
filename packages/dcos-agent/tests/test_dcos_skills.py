"""Tests for dcos-agent default skills + auto-registration."""

from __future__ import annotations

import json

import pytest

from agent_core.openbrain import OpenBrainStore, StubEmbeddingProvider
from agent_core.settings import AgentSettings
from agent_core.skills import (
    SkillContext,
    SkillRegistry,
    SkillRunner,
    StubLanguageModel,
    default_registry,
)
from agent_core.state import Database

import dcos_agent.skills  # noqa: F401 — triggers auto-registration
from dcos_agent.skills import DocumentCreator, EmailComposer, EmailTriage, register_defaults


# ── Fixtures ────────────────────────────────────────────────────────────────


def _ctx(*, lm: StubLanguageModel | None = None, with_openbrain: bool = False) -> SkillContext:
    db = Database.sqlite_memory()
    db.create_all()
    openbrain = (
        OpenBrainStore(db, StubEmbeddingProvider()) if with_openbrain else None
    )
    return SkillContext(
        settings=AgentSettings(),
        db=db,
        language_model=lm or StubLanguageModel(default=""),
        openbrain=openbrain,
    )


# ── Auto-registration ──────────────────────────────────────────────────────


def test_three_default_skills_auto_registered() -> None:
    for name in ("email-triage", "document-creator", "email-composer"):
        assert name in default_registry, f"{name} not auto-registered"


def test_register_defaults_idempotent() -> None:
    """Re-importing the module shouldn't crash; calling register_defaults
    twice on a registry that already has them is a no-op."""
    register_defaults()  # called once at import; safe to call again


def test_register_defaults_works_on_clean_registry() -> None:
    r = SkillRegistry()
    register_defaults(r)
    assert r.names() == ["document-creator", "email-composer", "email-triage"]


# ── email-triage ───────────────────────────────────────────────────────────


def test_triage_classifies_via_lm_response() -> None:
    lm = StubLanguageModel(
        default=json.dumps(
            {"action": "archive", "score": 0.92, "reasoning": "newsletter"}
        )
    )
    skill = EmailTriage()
    out = skill.execute(
        skill.input_schema(
            sender="news@example.com",
            subject="Daily digest",
            body="Today's top stories...",
        ),
        _ctx(lm=lm),
    )
    assert out.output.action == "archive"
    assert out.output.confidence_bucket == "high"
    assert out.confidence == pytest.approx(0.92)
    assert lm.calls[0]["system"].startswith("You are an email triage")


def test_triage_buckets_medium_and_low() -> None:
    skill = EmailTriage()
    for score, expected in ((0.65, "medium"), (0.30, "low")):
        lm = StubLanguageModel(
            default=json.dumps({"action": "flag", "score": score, "reasoning": "x"})
        )
        out = skill.execute(
            skill.input_schema(sender="a@b.c", subject="s", body="b"),
            _ctx(lm=lm),
        )
        assert out.output.confidence_bucket == expected


def test_triage_handles_fenced_json() -> None:
    """Models occasionally wrap JSON in ```json``` despite being told not to."""
    lm = StubLanguageModel(
        default=(
            "```json\n"
            + json.dumps({"action": "task", "score": 0.7, "reasoning": "x"})
            + "\n```"
        )
    )
    skill = EmailTriage()
    out = skill.execute(
        skill.input_schema(sender="a", subject="s", body="b"), _ctx(lm=lm)
    )
    assert out.output.action == "task"


def test_triage_rejects_unknown_action() -> None:
    lm = StubLanguageModel(
        default=json.dumps({"action": "yolo", "score": 0.9, "reasoning": "x"})
    )
    skill = EmailTriage()
    with pytest.raises(ValueError, match="unknown action"):
        skill.execute(
            skill.input_schema(sender="a", subject="s", body="b"), _ctx(lm=lm)
        )


def test_triage_rejects_empty_response() -> None:
    skill = EmailTriage()
    with pytest.raises(ValueError, match="empty"):
        skill.execute(
            skill.input_schema(sender="a", subject="s", body="b"),
            _ctx(lm=StubLanguageModel(default="")),
        )


def test_triage_seed_rules_present() -> None:
    """Seed rules ship with the skill so calibration isn't starting from zero."""
    skill = EmailTriage()
    assert len(skill.seed_rules) >= 2
    assert all(r.skill_tags == ["email-triage"] for r in skill.seed_rules)


# ── document-creator ───────────────────────────────────────────────────────


def test_document_creator_drafts_via_lm() -> None:
    body = (
        "Q3 results were strong: revenue up 14%, costs flat, margin "
        "expanded 200bps. Next: extend the same playbook into Q4."
    )
    lm = StubLanguageModel(default=body)
    skill = DocumentCreator()
    out = skill.execute(
        skill.input_schema(
            title="Q3 Recap",
            brief="One-paragraph summary of Q3",
            length_target="brief",
            ground_in_openbrain=False,
        ),
        _ctx(lm=lm),
    )
    assert out.output.title == "Q3 Recap"
    assert "revenue up" in out.output.body
    assert out.output.word_count > 0
    assert out.references == []


def test_document_creator_grounds_when_openbrain_present() -> None:
    """When ground_in_openbrain=True and openbrain has thoughts, those land
    in references on the result."""
    ctx = _ctx(
        lm=StubLanguageModel(default="Drafted body."),
        with_openbrain=True,
    )
    ctx.openbrain.capture(  # type: ignore[union-attr]
        "Q3 board meeting: discussed budget gap and rightsizing.",
        source_kind="vault",
        source_uri="vault/Q3-board.md",
    )
    skill = DocumentCreator()
    out = skill.execute(
        skill.input_schema(
            title="Q3 Recap",
            brief="Recap of Q3 board discussion",
            ground_in_openbrain=True,
        ),
        ctx,
    )
    assert len(out.references) >= 1
    assert out.references[0]["source_kind"] == "vault"


def test_document_creator_confidence_higher_when_grounded() -> None:
    """Ungrounded < grounded, all else equal."""
    ctx_un = _ctx(lm=StubLanguageModel(default="x" * 50))
    ctx_g = _ctx(lm=StubLanguageModel(default="x" * 50), with_openbrain=True)
    ctx_g.openbrain.capture(  # type: ignore[union-attr]
        "Relevant fact about the brief.", source_kind="vault"
    )
    skill = DocumentCreator()
    inp = skill.input_schema(
        title="t",
        brief="brief",
        length_target="brief",
        ground_in_openbrain=True,
    )
    out_un = skill.execute(
        skill.input_schema(
            title="t", brief="brief", length_target="brief", ground_in_openbrain=False
        ),
        ctx_un,
    )
    out_g = skill.execute(inp, ctx_g)
    assert out_g.confidence > out_un.confidence


def test_document_creator_seed_rules_present() -> None:
    assert len(DocumentCreator().seed_rules) >= 3


# ── email-composer ─────────────────────────────────────────────────────────


def test_email_composer_drafts_subject_and_body() -> None:
    lm = StubLanguageModel(
        default=(
            "SUBJECT: Re: Tuesday's question\n"
            "---\n"
            "Hi Charlotte,\n\n"
            "Thanks for the note. Yes, Tuesday at 2pm works.\n\n"
            "Best,\nAJ"
        )
    )
    skill = EmailComposer()
    out = skill.execute(
        skill.input_schema(
            to="Charlotte Grinberg",
            subject_hint="Re: Tuesday",
            brief="Confirm Tuesday at 2pm.",
            thread_history="Charlotte: Are you free Tuesday at 2?",
        ),
        _ctx(lm=lm),
    )
    assert out.output.subject == "Re: Tuesday's question"
    assert "Charlotte" in out.output.body
    assert out.output.word_count > 0


def test_email_composer_handles_missing_subject_with_fallback() -> None:
    """Format drift: model omits SUBJECT line — composer uses subject_hint."""
    lm = StubLanguageModel(default="Hi,\n\nQuick reply.\n\nBest,\nAJ")
    skill = EmailComposer()
    out = skill.execute(
        skill.input_schema(
            to="Sam", subject_hint="Re: lunch", brief="Decline lunch politely."
        ),
        _ctx(lm=lm),
    )
    assert out.output.subject == "Re: lunch"
    assert "Quick reply" in out.output.body


def test_email_composer_confidence_scales_with_signals() -> None:
    """Bare brief (no thread, no subject hint, no grounding) < rich context."""
    lm_default = "SUBJECT: x\n---\nbody"
    skill = EmailComposer()
    bare = skill.execute(
        skill.input_schema(to="x", brief="hi"),
        _ctx(lm=StubLanguageModel(default=lm_default)),
    )
    rich = skill.execute(
        skill.input_schema(
            to="x",
            brief="hi",
            subject_hint="Re: x",
            thread_history="prior",
        ),
        _ctx(lm=StubLanguageModel(default=lm_default)),
    )
    assert rich.confidence > bare.confidence


def test_email_composer_rejects_empty_response() -> None:
    skill = EmailComposer()
    with pytest.raises(ValueError, match="empty"):
        skill.execute(
            skill.input_schema(to="x", brief="hi"),
            _ctx(lm=StubLanguageModel(default="")),
        )


def test_email_composer_rejects_empty_body() -> None:
    skill = EmailComposer()
    with pytest.raises(ValueError, match="empty body"):
        skill.execute(
            skill.input_schema(to="x", brief="hi"),
            _ctx(lm=StubLanguageModel(default="SUBJECT: only\n---\n")),
        )


def test_email_composer_seed_rules_include_formality_match() -> None:
    rules = EmailComposer().seed_rules
    assert any("formality" in r.correction.lower() for r in rules)


# ── End-to-end via SkillRunner ────────────────────────────────────────────


def test_runner_executes_email_triage_end_to_end() -> None:
    runner = SkillRunner(default_registry)
    lm = StubLanguageModel(
        default=json.dumps({"action": "flag", "score": 0.85, "reasoning": "user-q"})
    )
    outcome = runner.run(
        "email-triage",
        {
            "sender": "boss@example.com",
            "subject": "Quick question",
            "body": "Can you send the Q3 numbers?",
        },
        _ctx(lm=lm),
    )
    assert outcome.succeeded, outcome.error
    assert outcome.result.output.action == "flag"
