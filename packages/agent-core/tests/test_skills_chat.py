"""Tests for agent_core.skills.chat — context-injected REPL turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent_core.openbrain import OpenBrainStore, StubEmbeddingProvider
from agent_core.skills import StubLanguageModel
from agent_core.skills.chat import (
    DEFAULT_SYSTEM_PROMPT,
    ChatMessage,
    ChatSession,
    build_context_prompt,
    run_turn,
)
from agent_core.state import Database
from agent_core.state.models import (
    Obligation,
    ObligationOwner,
    ObligationSource,
    ObligationStatus,
)


def _db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


# ── ChatSession ────────────────────────────────────────────────────────────


def test_session_starts_empty() -> None:
    s = ChatSession()
    assert s.history == []
    assert s.inject_obligations is True
    assert s.inject_openbrain is True


def test_session_append_records_messages() -> None:
    s = ChatSession()
    s.append("user", "hello")
    s.append("assistant", "hi there")
    assert len(s.history) == 2
    assert s.history[0] == ChatMessage(role="user", content="hello")
    assert s.history[1] == ChatMessage(role="assistant", content="hi there")


def test_session_reset_clears_history_keeps_settings() -> None:
    s = ChatSession(inject_openbrain=False, openbrain_hits=7)
    s.append("user", "x")
    s.reset()
    assert s.history == []
    assert s.inject_openbrain is False
    assert s.openbrain_hits == 7


# ── build_context_prompt ───────────────────────────────────────────────────


def test_build_context_prompt_no_extras_returns_base() -> None:
    out = build_context_prompt(base_system="be concise.")
    assert out == "be concise."


def test_build_context_prompt_includes_obligations() -> None:
    @dataclass
    class _StubOb:
        title: str
        body: str | None
        status: Any

    obs = [
        _StubOb(title="Review CMS evaluation", body="v6 awaiting AJ approval", status="in-progress"),
        _StubOb(title="Charlotte dinner", body=None, status="inbox"),
    ]
    out = build_context_prompt(base_system="x", obligations=obs)
    assert "Currently active obligations" in out
    assert "Review CMS evaluation" in out
    assert "v6 awaiting AJ approval" in out
    assert "Charlotte dinner" in out


def test_build_context_prompt_includes_openbrain_hits() -> None:
    @dataclass
    class _StubSrc:
        source_kind: str

    @dataclass
    class _StubThought:
        content: str

    @dataclass
    class _StubHit:
        thought: _StubThought
        sources: list[_StubSrc]
        similarity: float

    hits = [
        _StubHit(
            thought=_StubThought(content="Q3 board meeting notes"),
            sources=[_StubSrc(source_kind="vault")],
            similarity=0.87,
        )
    ]
    out = build_context_prompt(base_system="x", openbrain_hits=hits)
    assert "semantic memory" in out
    assert "Q3 board meeting notes" in out
    assert "source: vault" in out


def test_build_context_prompt_omits_empty_sections() -> None:
    """Empty obligations / hits don't add a 'no current obligations' line —
    saves tokens."""
    out = build_context_prompt(base_system="x", obligations=[], openbrain_hits=[])
    assert out == "x"


# ── run_turn ───────────────────────────────────────────────────────────────


def test_run_turn_calls_llm_and_records_history() -> None:
    lm = StubLanguageModel(default="agent reply")
    session = ChatSession(inject_obligations=False, inject_openbrain=False)
    reply = run_turn(
        user_message="hello",
        session=session,
        language_model=lm,
    )
    assert reply == "agent reply"
    assert len(session.history) == 2
    assert session.history[-1].role == "assistant"
    assert session.history[-1].content == "agent reply"
    assert len(lm.calls) == 1


def test_run_turn_includes_history_in_user_prompt() -> None:
    lm = StubLanguageModel(default="ok")
    session = ChatSession(inject_obligations=False, inject_openbrain=False)
    run_turn(user_message="first message", session=session, language_model=lm)
    run_turn(user_message="second message", session=session, language_model=lm)
    # The second turn's user-prompt should reference the first turn
    second_call_user = lm.calls[1]["user"]
    assert "first message" in second_call_user
    assert "second message" in second_call_user


def test_run_turn_injects_obligations() -> None:
    db = _db()
    with db.session() as s:
        s.add(
            Obligation(
                title="Q3 budget review",
                body="Charlotte raised the gap",
                source=ObligationSource.manual,
                status=ObligationStatus.in_progress,
            )
        )
        s.commit()
    lm = StubLanguageModel(default="ok")
    session = ChatSession(inject_obligations=True, inject_openbrain=False)
    run_turn(
        user_message="what am I working on?",
        session=session,
        language_model=lm,
        db=db,
    )
    system_prompt = lm.calls[0]["system"]
    assert "Q3 budget review" in system_prompt


def test_run_turn_excludes_done_obligations() -> None:
    """Don't inject 'done' obligations — only active ones."""
    db = _db()
    with db.session() as s:
        s.add(
            Obligation(
                title="ACTIVE-thing",
                source=ObligationSource.manual,
                status=ObligationStatus.in_progress,
            )
        )
        s.add(
            Obligation(
                title="OLD-thing",
                source=ObligationSource.manual,
                status=ObligationStatus.done,
            )
        )
        s.commit()
    lm = StubLanguageModel(default="ok")
    session = ChatSession(inject_obligations=True, inject_openbrain=False)
    run_turn(user_message="x", session=session, language_model=lm, db=db)
    system = lm.calls[0]["system"]
    assert "ACTIVE-thing" in system
    assert "OLD-thing" not in system


def test_run_turn_injects_openbrain_hits() -> None:
    db = _db()
    store = OpenBrainStore(db, StubEmbeddingProvider())
    store.capture(
        "Charlotte mentioned the Q3 budget gap on Tuesday", source_kind="gmail"
    )
    lm = StubLanguageModel(default="ok")
    session = ChatSession(inject_obligations=False, inject_openbrain=True)
    run_turn(
        user_message="what about Charlotte and the budget?",
        session=session,
        language_model=lm,
        db=db,
        openbrain=store,
    )
    system = lm.calls[0]["system"]
    assert "semantic memory" in system
    assert "Charlotte mentioned" in system


def test_run_turn_disables_injection_when_session_says_no() -> None:
    db = _db()
    with db.session() as s:
        s.add(Obligation(title="should-not-inject", source=ObligationSource.manual))
        s.commit()
    lm = StubLanguageModel(default="ok")
    session = ChatSession(inject_obligations=False, inject_openbrain=False)
    run_turn(user_message="anything", session=session, language_model=lm, db=db)
    assert "should-not-inject" not in lm.calls[0]["system"]


def test_run_turn_uses_default_system_prompt_when_session_blank() -> None:
    lm = StubLanguageModel(default="ok")
    session = ChatSession()  # system_prompt == ""
    run_turn(user_message="x", session=session, language_model=lm)
    assert "digital chief of staff" in lm.calls[0]["system"]


def test_run_turn_respects_custom_system_prompt() -> None:
    lm = StubLanguageModel(default="ok")
    session = ChatSession(system_prompt="You are a haiku poet.")
    run_turn(user_message="x", session=session, language_model=lm)
    assert "haiku poet" in lm.calls[0]["system"]
    assert DEFAULT_SYSTEM_PROMPT not in lm.calls[0]["system"]


def test_run_turn_handles_openbrain_failure_gracefully() -> None:
    """An openbrain search failure should NOT break the chat turn —
    just skip the injection."""
    class _BrokenStore:
        def search(self, *args, **kwargs):
            raise RuntimeError("storage went away")

    lm = StubLanguageModel(default="ok")
    session = ChatSession(inject_obligations=False, inject_openbrain=True)
    reply = run_turn(
        user_message="x",
        session=session,
        language_model=lm,
        openbrain=_BrokenStore(),
    )
    assert reply == "ok"  # turn completed despite openbrain failure
