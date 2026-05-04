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


def test_build_context_prompt_includes_calendar_events() -> None:
    """Sprint 23: today's events appear in the system prompt."""
    from datetime import datetime, timezone

    from agent_core.work.calendar import CalendarEvent

    events = [
        CalendarEvent(
            uid="1",
            summary="Q2 review",
            start=datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 4, 15, 0, tzinfo=timezone.utc),
            location="Zoom",
        ),
        CalendarEvent(
            uid="2",
            summary="Off site day",
            start=datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc),
            all_day=True,
        ),
    ]
    out = build_context_prompt(base_system="x", calendar_events=events)
    assert "Today's calendar" in out
    assert "Q2 review" in out
    assert "14:00" in out  # time-of-day rendering
    assert "Zoom" in out
    assert "(all day)" in out
    assert "Off site day" in out


def test_build_context_prompt_omits_empty_calendar_section() -> None:
    out = build_context_prompt(base_system="x", calendar_events=[])
    assert out == "x"
    assert "Today's calendar" not in out


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
    """Legacy single-shot path: when tool-use is disabled, history flattens
    into the user prompt. (Sprint 24+: with tools enabled, history is
    passed as separate messages — see test_run_turn_tools_pass_history.)"""
    lm = StubLanguageModel(default="ok")
    session = ChatSession(
        inject_obligations=False,
        inject_openbrain=False,
        enable_tools=False,
    )
    run_turn(user_message="first message", session=session, language_model=lm)
    run_turn(user_message="second message", session=session, language_model=lm)
    # The second turn's user-prompt should reference the first turn
    second_call_user = lm.calls[1]["user"]
    assert "first message" in second_call_user
    assert "second message" in second_call_user


def test_run_turn_tools_pass_history_as_messages() -> None:
    """Sprint 24: with tools enabled, history is passed to the LM as
    proper OpenAI-format messages, not flattened into the user prompt."""
    lm = StubLanguageModel(default="ok")
    session = ChatSession(inject_obligations=False, inject_openbrain=False)
    run_turn(user_message="first message", session=session, language_model=lm)
    run_turn(user_message="second message", session=session, language_model=lm)

    # The second tool-call should have BOTH first/second messages as separate
    # entries, plus the prior assistant reply.
    second = lm.tool_calls_recorded[1]
    user_messages = [m for m in second["messages"] if m.get("role") == "user"]
    user_contents = [m["content"] for m in user_messages]
    assert "first message" in user_contents
    assert "second message" in user_contents


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

        def capture(self, *args, **kwargs):
            raise RuntimeError("capture also broken")

    lm = StubLanguageModel(default="ok")
    session = ChatSession(inject_obligations=False, inject_openbrain=True)
    reply = run_turn(
        user_message="x",
        session=session,
        language_model=lm,
        openbrain=_BrokenStore(),
    )
    assert reply == "ok"  # turn completed despite openbrain failure


# ── Cross-session memory (auto-capture to OpenBrain) ──────────────────────


def test_run_turn_auto_captures_to_openbrain() -> None:
    """Default behavior: each turn lands as a Thought with source_kind='chat'."""
    db = _db()
    store = OpenBrainStore(db, StubEmbeddingProvider())
    lm = StubLanguageModel(default="agent reply text")
    session = ChatSession(
        inject_obligations=False,
        inject_openbrain=False,  # avoid feedback loop in this test
        session_id="test-session-abc",
    )

    run_turn(
        user_message="ask about Q3 budget",
        session=session,
        language_model=lm,
        db=db,
        openbrain=store,
    )

    thoughts = store.recent(limit=10)
    assert len(thoughts) == 1
    t = thoughts[0]
    assert "ask about Q3 budget" in t.content
    assert "agent reply text" in t.content


def test_run_turn_capture_includes_source_provenance() -> None:
    """Captured chat turns get source_kind='chat' + source_uri=session_id
    so they're filterable + groupable later."""
    from sqlmodel import select

    from agent_core.state.models import ThoughtSource

    db = _db()
    store = OpenBrainStore(db, StubEmbeddingProvider())
    session = ChatSession(
        inject_obligations=False,
        inject_openbrain=False,
        session_id="cli-12345",
    )
    run_turn(
        user_message="hi",
        session=session,
        language_model=StubLanguageModel(default="hi back"),
        db=db,
        openbrain=store,
    )

    with db.session() as s:
        sources = list(s.exec(select(ThoughtSource)).all())
    assert len(sources) == 1
    assert sources[0].source_kind == "chat"
    assert sources[0].source_uri == "cli-12345"
    assert sources[0].source_title == "chat turn"


def test_run_turn_record_disabled_skips_capture() -> None:
    """record_to_openbrain=False → no Thought lands in the store."""
    db = _db()
    store = OpenBrainStore(db, StubEmbeddingProvider())
    session = ChatSession(
        inject_obligations=False,
        inject_openbrain=False,
        record_to_openbrain=False,
    )
    run_turn(
        user_message="don't remember this",
        session=session,
        language_model=StubLanguageModel(default="ok"),
        db=db,
        openbrain=store,
    )
    assert store.recent(limit=10) == []


def test_run_turn_no_openbrain_no_capture() -> None:
    """If openbrain=None, nothing's captured (and the turn doesn't crash)."""
    session = ChatSession(record_to_openbrain=True)  # but no store passed
    reply = run_turn(
        user_message="x",
        session=session,
        language_model=StubLanguageModel(default="ok"),
        openbrain=None,
    )
    assert reply == "ok"


def test_run_turn_capture_failure_doesnt_break_turn() -> None:
    """OpenBrain capture errors should be swallowed — never fail the turn."""

    class _PartialStore:
        """Search works, capture raises."""

        def search(self, *args, **kwargs):
            return []

        def capture(self, *args, **kwargs):
            raise RuntimeError("write failure")

    session = ChatSession(inject_obligations=False)
    reply = run_turn(
        user_message="x",
        session=session,
        language_model=StubLanguageModel(default="ok"),
        openbrain=_PartialStore(),
    )
    assert reply == "ok"  # turn succeeded despite capture failure


def test_chat_memory_searchable_in_followup_turn() -> None:
    """End-to-end: turn 1 captures to OpenBrain → turn 2's context-injection
    surfaces it in the system prompt."""
    from agent_core.openbrain.embeddings import SemanticStubProvider

    db = _db()
    # Use SemanticStub for content-aware similarity (StubEmbeddingProvider
    # is hash-based; doesn't surface semantically-related content).
    store = OpenBrainStore(db, SemanticStubProvider())
    lm = StubLanguageModel(default="acknowledged")

    s1 = ChatSession(inject_obligations=False, session_id="t1")
    run_turn(
        user_message="The Q3 budget gap is $500k",
        session=s1,
        language_model=lm,
        openbrain=store,
    )

    # Different session, fresh history — should still find the prior turn
    s2 = ChatSession(inject_obligations=False, session_id="t2", inject_openbrain=True)
    run_turn(
        user_message="What was that Q3 budget number?",
        session=s2,
        language_model=lm,
        openbrain=store,
    )

    # The second turn's system prompt should include the prior conversation
    second_call = lm.calls[-1]
    assert "Q3 budget gap is $500k" in second_call["system"]
