"""Tests for agent_core.skills — framework (registry / runner / stubs)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agent_core.settings import AgentSettings
from agent_core.skills import (
    SeedRule,
    SkillContext,
    SkillRegistry,
    SkillResult,
    SkillRunner,
    StubLanguageModel,
)
from agent_core.state import Database


# ── Fixture skill ──────────────────────────────────────────────────────────


class _EchoInput(BaseModel):
    text: str


class _EchoOutput(BaseModel):
    echoed: str


class _EchoSkill:
    """Tiny test skill — echoes input.text back as output.echoed."""

    name = "echo"
    description = "echo a string"
    tags = ["test", "echo"]
    input_schema = _EchoInput
    output_schema = _EchoOutput
    seed_rules = [SeedRule(correction="be honest", skill_tags=["echo"])]

    def execute(self, input: _EchoInput, context: SkillContext) -> SkillResult:
        return SkillResult(
            output=_EchoOutput(echoed=input.text),
            confidence=0.9,
            rationale="echoed",
        )


def _ctx() -> SkillContext:
    db = Database.sqlite_memory()
    db.create_all()
    return SkillContext(
        settings=AgentSettings(),
        db=db,
        language_model=StubLanguageModel(default="ok"),
    )


# ── Registry ───────────────────────────────────────────────────────────────


def test_registry_register_and_get() -> None:
    r = SkillRegistry()
    skill = _EchoSkill()
    r.register(skill)
    assert r.get("echo") is skill
    assert "echo" in r
    assert len(r) == 1


def test_registry_rejects_duplicate_names() -> None:
    r = SkillRegistry()
    r.register(_EchoSkill())
    with pytest.raises(ValueError, match="already registered"):
        r.register(_EchoSkill())


def test_registry_rejects_empty_name() -> None:
    r = SkillRegistry()

    class _NoName:
        name = ""
        description = "x"
        tags: list = []
        input_schema = _EchoInput
        output_schema = _EchoOutput
        seed_rules: list = []

        def execute(self, input, context):  # pragma: no cover
            ...

    with pytest.raises(ValueError, match="non-empty"):
        r.register(_NoName())


def test_registry_require_helpful_error() -> None:
    r = SkillRegistry()
    r.register(_EchoSkill())
    with pytest.raises(KeyError, match="missing"):
        r.require("missing")


def test_registry_by_tag() -> None:
    r = SkillRegistry()
    r.register(_EchoSkill())
    assert len(r.by_tag("echo")) == 1
    assert r.by_tag("nope") == []


def test_registry_unregister() -> None:
    r = SkillRegistry()
    r.register(_EchoSkill())
    r.unregister("echo")
    assert r.get("echo") is None
    assert len(r) == 0


# ── Runner ─────────────────────────────────────────────────────────────────


def test_runner_executes_registered_skill() -> None:
    r = SkillRegistry()
    r.register(_EchoSkill())
    runner = SkillRunner(r)
    outcome = runner.run("echo", {"text": "hello"}, _ctx())
    assert outcome.succeeded
    assert outcome.result is not None
    assert outcome.result.output.echoed == "hello"
    assert outcome.result.confidence == 0.9


def test_runner_unknown_skill() -> None:
    runner = SkillRunner(SkillRegistry())
    outcome = runner.run("nope", {}, _ctx())
    assert not outcome.succeeded
    assert "not registered" in (outcome.error or "")


def test_runner_input_validation_fails_loud() -> None:
    r = SkillRegistry()
    r.register(_EchoSkill())
    runner = SkillRunner(r)
    outcome = runner.run("echo", {"wrong_field": 1}, _ctx())
    assert not outcome.succeeded
    assert "input failed validation" in (outcome.error or "")


def test_runner_catches_skill_exception() -> None:
    """A crashing skill must not take down the caller."""

    class _Boom:
        name = "boom"
        description = "raises"
        tags: list = []
        input_schema = _EchoInput
        output_schema = _EchoOutput
        seed_rules: list = []

        def execute(self, input, context):
            raise RuntimeError("intentional")

    r = SkillRegistry()
    r.register(_Boom())
    runner = SkillRunner(r)
    outcome = runner.run("boom", {"text": "x"}, _ctx())
    assert not outcome.succeeded
    assert "intentional" in (outcome.error or "")


def test_runner_validates_skill_output() -> None:
    """Skill returning the wrong shape fails at the boundary, not later."""

    class _WrongOutputs(BaseModel):
        unexpected: str

    class _BadSkill:
        name = "bad"
        description = "x"
        tags: list = []
        input_schema = _EchoInput
        output_schema = _EchoOutput

        seed_rules: list = []

        def execute(self, input, context):
            # Returns the wrong Pydantic model
            return SkillResult(
                output=_WrongOutputs(unexpected="hi"), confidence=0.5, rationale=""
            )

    r = SkillRegistry()
    r.register(_BadSkill())
    runner = SkillRunner(r)
    outcome = runner.run("bad", {"text": "x"}, _ctx())
    assert not outcome.succeeded
    assert "output failed validation" in (outcome.error or "")


# ── StubLanguageModel ──────────────────────────────────────────────────────


def test_stub_language_model_returns_default() -> None:
    lm = StubLanguageModel(default="hi")
    assert lm.complete(system="x", user="y") == "hi"
    assert len(lm.calls) == 1


def test_stub_language_model_cycles_responses() -> None:
    lm = StubLanguageModel(responses=["one", "two"])
    assert lm.complete(system="x", user="y") == "one"
    assert lm.complete(system="x", user="y") == "two"
    assert lm.complete(system="x", user="y") == "one"  # wraps


def test_stub_language_model_pattern_match() -> None:
    lm = StubLanguageModel(
        patterns=[("triage", "{\"action\": \"flag\", \"score\": 0.9}")],
        default="other",
    )
    assert "flag" in lm.complete(system="email triage classifier", user="x")
    assert lm.complete(system="other", user="x") == "other"


def test_stub_records_all_calls() -> None:
    lm = StubLanguageModel(default="x")
    lm.complete(system="s1", user="u1")
    lm.complete(system="s2", user="u2", temperature=0.5)
    assert len(lm.calls) == 2
    assert lm.calls[0]["system"] == "s1"
    assert lm.calls[1]["temperature"] == 0.5
