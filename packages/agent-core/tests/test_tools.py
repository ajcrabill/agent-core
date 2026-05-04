"""Sprint 24 — tool-use plumbing tests.

Coverage:
  * ToolDefinition.to_openai_format
  * Built-in tool handlers against in-memory DB / OpenBrain / calendar stubs
  * execute_tool_call dispatch + error handling
  * run_tool_loop with canned tool_call responses
  * Fallback to plain complete() when LM has no complete_with_tools
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_core.skills import StubLanguageModel
from agent_core.skills.tools import (
    CompletionResponse,
    ToolCall,
    ToolContext,
    ToolDefinition,
    default_read_tools,
    execute_tool_call,
    run_tool_loop,
)
from agent_core.state.db import Database
from agent_core.state.models import Obligation, ObligationSource, ObligationStatus
from agent_core.work.calendar import CalendarEvent


UTC = timezone.utc


# ── ToolDefinition surface ─────────────────────────────────────────────────


def test_tool_definition_to_openai_format():
    tool = ToolDefinition(
        name="hello",
        description="say hi",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        handler=lambda args, ctx: {"greeting": f"hi {args['name']}"},
    )
    fmt = tool.to_openai_format()
    assert fmt["type"] == "function"
    assert fmt["function"]["name"] == "hello"
    assert fmt["function"]["description"] == "say hi"
    assert fmt["function"]["parameters"]["type"] == "object"


# ── Built-in tools: list_obligations ───────────────────────────────────────


def _db_with_obligations() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(
            Obligation(
                title="Reply to boss about Q2",
                source=ObligationSource.inbound_email,
                status=ObligationStatus.inbox,
                priority=10,
            )
        )
        s.add(
            Obligation(
                title="Review Charlotte's PR",
                source=ObligationSource.manual,
                status=ObligationStatus.in_progress,
                priority=5,
            )
        )
        s.add(
            Obligation(
                title="finished old thing",
                source=ObligationSource.manual,
                status=ObligationStatus.done,
                priority=0,
            )
        )
        s.commit()
    return db


def test_list_obligations_returns_active():
    db = _db_with_obligations()
    tools = default_read_tools()
    tool = next(t for t in tools if t.name == "list_obligations")
    result = tool.handler({}, ToolContext(db=db))
    assert result["count"] == 2  # done excluded
    titles = [o["title"] for o in result["obligations"]]
    assert "Reply to boss about Q2" in titles
    assert "Review Charlotte's PR" in titles
    # Higher priority listed first
    assert result["obligations"][0]["title"] == "Reply to boss about Q2"


def test_list_obligations_filters_by_status():
    db = _db_with_obligations()
    tools = default_read_tools()
    tool = next(t for t in tools if t.name == "list_obligations")
    result = tool.handler({"status": "inbox"}, ToolContext(db=db))
    assert result["count"] == 1
    assert result["obligations"][0]["status"] == "inbox"


def test_list_obligations_returns_error_on_unknown_status():
    db = _db_with_obligations()
    tools = default_read_tools()
    tool = next(t for t in tools if t.name == "list_obligations")
    result = tool.handler({"status": "bogus"}, ToolContext(db=db))
    assert "error" in result
    assert "bogus" in result["error"]


def test_list_obligations_handles_missing_db():
    tools = default_read_tools()
    tool = next(t for t in tools if t.name == "list_obligations")
    result = tool.handler({}, ToolContext(db=None))
    assert "error" in result


# ── search_memory ──────────────────────────────────────────────────────────


class _FakeOpenbrain:
    def __init__(self, hits):
        self._hits = hits
        self.calls = []

    def search(self, query, limit):
        self.calls.append((query, limit))
        return self._hits


class _FakeHit:
    def __init__(self, content, similarity, source_kind=None):
        class _T:
            pass

        self.thought = _T()
        self.thought.content = content
        self.similarity = similarity
        if source_kind:
            class _S:
                pass

            src = _S()
            src.source_kind = source_kind
            self.sources = [src]
        else:
            self.sources = []


def test_search_memory_returns_hits():
    ob = _FakeOpenbrain([_FakeHit("note about Q2", 0.9, "vault")])
    tools = default_read_tools()
    tool = next(t for t in tools if t.name == "search_memory")
    result = tool.handler({"query": "Q2 budget"}, ToolContext(openbrain=ob))
    assert result["count"] == 1
    assert "note about Q2" in result["hits"][0]["content"]
    assert result["hits"][0]["similarity"] == 0.9
    assert result["hits"][0]["source_kind"] == "vault"
    assert ob.calls == [("Q2 budget", 3)]


def test_search_memory_requires_query():
    tools = default_read_tools()
    tool = next(t for t in tools if t.name == "search_memory")
    result = tool.handler({}, ToolContext(openbrain=_FakeOpenbrain([])))
    assert "error" in result
    assert "query" in result["error"]


def test_search_memory_handles_missing_openbrain():
    tools = default_read_tools()
    tool = next(t for t in tools if t.name == "search_memory")
    result = tool.handler({"query": "x"}, ToolContext(openbrain=None))
    assert "error" in result


# ── today_calendar / upcoming_calendar ─────────────────────────────────────


class _FakeCalendar:
    def __init__(self, events):
        self._events = events

    def fetch_events(self, *, start, end):
        # Return events that overlap [start, end)
        return [
            e for e in self._events
            if e.start < end and e.end > start
        ]


def test_today_calendar_returns_events():
    today_morning = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0)
    cal = _FakeCalendar([
        CalendarEvent(
            uid="1",
            summary="Q2 review",
            start=today_morning,
            end=today_morning + timedelta(hours=1),
            location="Zoom",
        ),
    ])
    tools = default_read_tools()
    tool = next(t for t in tools if t.name == "today_calendar")
    result = tool.handler({}, ToolContext(calendar=cal))
    assert result["count"] == 1
    assert result["events"][0]["summary"] == "Q2 review"
    assert result["events"][0]["location"] == "Zoom"


def test_upcoming_calendar_uses_hours_arg():
    now = datetime.now(UTC)
    cal = _FakeCalendar([
        CalendarEvent(
            uid="1", summary="soon",
            start=now + timedelta(hours=2),
            end=now + timedelta(hours=3),
        ),
        CalendarEvent(
            uid="2", summary="far",
            start=now + timedelta(hours=200),
            end=now + timedelta(hours=201),
        ),
    ])
    tools = default_read_tools()
    tool = next(t for t in tools if t.name == "upcoming_calendar")
    result = tool.handler({"hours": 24}, ToolContext(calendar=cal))
    assert result["count"] == 1
    assert result["events"][0]["summary"] == "soon"


def test_calendar_tools_handle_missing_calendar():
    tools = default_read_tools()
    today = next(t for t in tools if t.name == "today_calendar")
    upcoming = next(t for t in tools if t.name == "upcoming_calendar")
    assert "error" in today.handler({}, ToolContext(calendar=None))
    assert "error" in upcoming.handler({}, ToolContext(calendar=None))


# ── execute_tool_call ──────────────────────────────────────────────────────


def test_execute_tool_call_dispatches_and_returns_json():
    tools = [
        ToolDefinition(
            name="echo",
            description="x",
            input_schema={"type": "object"},
            handler=lambda args, ctx: {"got": args},
        )
    ]
    call = ToolCall(id="c1", name="echo", arguments={"x": 1})
    result = execute_tool_call(call, tools, ToolContext())
    assert json.loads(result) == {"got": {"x": 1}}


def test_execute_tool_call_unknown_tool_returns_error():
    call = ToolCall(id="c1", name="not_real", arguments={})
    result = execute_tool_call(call, [], ToolContext())
    parsed = json.loads(result)
    assert "error" in parsed
    assert "not_real" in parsed["error"]


def test_execute_tool_call_catches_handler_exceptions():
    def _boom(args, ctx):
        raise ValueError("bad")

    tools = [
        ToolDefinition(
            name="boom",
            description="",
            input_schema={"type": "object"},
            handler=_boom,
        )
    ]
    call = ToolCall(id="c1", name="boom", arguments={})
    result = execute_tool_call(call, tools, ToolContext())
    parsed = json.loads(result)
    assert "error" in parsed
    assert "ValueError" in parsed["error"]


# ── run_tool_loop ──────────────────────────────────────────────────────────


def test_run_tool_loop_returns_text_when_no_tool_calls():
    lm = StubLanguageModel(
        tool_call_responses=[CompletionResponse(content="hello there", tool_calls=[])]
    )
    out = run_tool_loop(
        language_model=lm,
        system="be helpful",
        user_message="hi",
        history=[],
        tools=default_read_tools(),
        context=ToolContext(),
    )
    assert out == "hello there"


def test_run_tool_loop_executes_and_continues():
    """First response calls a tool; second response returns text using
    the tool result. Verifies the loop drives the conversation forward."""
    db = _db_with_obligations()
    captured_tool_args: list = []

    def _on(name, args):
        captured_tool_args.append((name, args))

    lm = StubLanguageModel(
        tool_call_responses=[
            CompletionResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="c1", name="list_obligations", arguments={"limit": 5})
                ],
            ),
            CompletionResponse(
                content="You have two open obligations.", tool_calls=[]
            ),
        ]
    )
    out = run_tool_loop(
        language_model=lm,
        system="be helpful",
        user_message="what's on my plate?",
        history=[],
        tools=default_read_tools(),
        context=ToolContext(db=db),
        on_tool_call=_on,
    )
    assert out == "You have two open obligations."
    assert captured_tool_args == [("list_obligations", {"limit": 5})]
    # Second LM call should have included a 'tool' message with the result
    second = lm.tool_calls_recorded[1]
    tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "c1"


def test_run_tool_loop_falls_back_when_lm_lacks_tool_support():
    """Pre-Sprint-24 LMs without complete_with_tools fall through to
    plain complete() — no crash, no tool execution."""

    class _OldLM:
        def complete(self, *, system, user, max_tokens=2048, temperature=0.0):
            return f"legacy: {user[:20]}"

    out = run_tool_loop(
        language_model=_OldLM(),
        system="x",
        user_message="hi there",
        history=[],
        tools=default_read_tools(),
        context=ToolContext(),
    )
    assert out.startswith("legacy:")


def test_run_tool_loop_max_iterations_safety():
    """A model that won't stop calling tools should be bounded."""
    # Always emit a tool_call, never a final content
    forever_calls = [
        CompletionResponse(
            content=None,
            tool_calls=[ToolCall(id=f"c{i}", name="list_obligations", arguments={})],
        )
        for i in range(20)
    ]
    db = _db_with_obligations()
    lm = StubLanguageModel(tool_call_responses=forever_calls)
    out = run_tool_loop(
        language_model=lm,
        system="x",
        user_message="loop",
        history=[],
        tools=default_read_tools(),
        context=ToolContext(db=db),
        max_iterations=3,
    )
    assert "exceeded" in out.lower() or out == ""
    # Three iterations max → 3 tool calls executed
    assert len([c for c in lm.tool_calls_recorded]) == 3


def test_run_tool_loop_handles_lm_exception():
    class _BoomLM:
        def complete_with_tools(self, **kwargs):
            raise RuntimeError("network died")

    out = run_tool_loop(
        language_model=_BoomLM(),
        system="x",
        user_message="hi",
        history=[],
        tools=default_read_tools(),
        context=ToolContext(),
    )
    assert "LLM call failed" in out
    assert "network died" in out


def test_run_tool_loop_passes_history_into_messages():
    lm = StubLanguageModel(
        tool_call_responses=[CompletionResponse(content="ok", tool_calls=[])]
    )
    history = [
        {"role": "user", "content": "earlier user"},
        {"role": "assistant", "content": "earlier assistant"},
    ]
    run_tool_loop(
        language_model=lm,
        system="x",
        user_message="now",
        history=history,
        tools=default_read_tools(),
        context=ToolContext(),
    )
    msgs = lm.tool_calls_recorded[0]["messages"]
    assert msgs[0]["content"] == "earlier user"
    assert msgs[1]["content"] == "earlier assistant"
    assert msgs[2]["content"] == "now"


# ── StubLanguageModel.complete_with_tools fallback ─────────────────────────


def test_stub_complete_with_tools_falls_back_to_default_text():
    """Once tool_call_responses is exhausted, returns content using
    complete()'s default."""
    lm = StubLanguageModel(default="fallback content")
    resp = lm.complete_with_tools(
        system="x", messages=[{"role": "user", "content": "hi"}], tools=[]
    )
    assert resp.content == "fallback content"
    assert resp.tool_calls == []
