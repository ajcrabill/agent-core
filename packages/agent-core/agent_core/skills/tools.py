"""Tool-use plumbing for chat: turn agent capabilities into LLM tools.

Sprint 24: makes chat *do* things, not just *talk* about them. When the
LLM asks "what's on my plate?" we now hand it tools to actually look up
obligations, search memory, and read the calendar — and it composes the
answer from real data instead of guessing.

Architecture:

  - ``ToolDefinition`` — name + description + JSON Schema + handler.
  - ``ToolRegistry.default_for(...)`` — assembles the read-only tool
    set (list_obligations, search_memory, today_calendar,
    upcoming_calendar) bound to a ToolContext.
  - ``CompletionResponse`` — what ``LanguageModel.complete_with_tools()``
    returns: either ``content`` text OR ``tool_calls`` to execute.
  - ``run_tool_loop()`` — orchestrator. Calls the LLM with tools; when
    the LLM emits tool_calls, executes them and re-calls. Bounded by
    ``max_iterations`` so a runaway model can't burn budget.

Safety: this MVP exposes READ-only tools. Write tools (compose drafts,
capture obligations, send email) will land in a follow-on sprint with
explicit per-call confirmation prompts so the user stays in the loop on
anything irreversible.

Failure modes:
  - LLM doesn't support tool-calls (older model, stub) → falls back to
    plain ``complete()`` and the answer is whatever the model invents.
  - Tool handler raises → we serialize the error back to the model so
    it can recover or apologize gracefully.
  - max_iterations exceeded → we return the last text content (or a
    canned "exceeded" message) without hanging.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Data ────────────────────────────────────────────────────────────────────


@dataclass
class ToolContext:
    """Wires available agent infra into tool handlers.

    Each handler picks what it needs out of this. Handlers should treat
    None values as "feature not configured" and degrade gracefully.
    """

    db: Any | None = None
    openbrain: Any | None = None
    calendar: Any | None = None
    settings: Any | None = None


@dataclass
class ToolDefinition:
    """One callable capability advertised to the LLM.

    The ``input_schema`` is a JSON Schema dict (OpenAI's tools API
    expects exactly that). The ``handler`` receives the parsed
    arguments and a ToolContext, returns either a string or a
    JSON-serializable dict.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, ToolContext], Any]
    safety: str = "read"  # "read" | "write_with_confirm" (writes land later)

    def to_openai_format(self) -> dict:
        """OpenAI Chat Completions tools format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str  # OpenAI's tool_call_id, used to match the tool message back
    name: str
    arguments: dict


@dataclass
class CompletionResponse:
    """Either-or: the LLM either returned content OR requested tool calls.

    A response with both is technically possible (OpenAI sometimes returns
    a brief content message alongside tool calls); we surface both
    without losing either.
    """

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


# ── Built-in tools (read-only) ─────────────────────────────────────────────


def _list_obligations_handler(args: dict, ctx: ToolContext) -> dict:
    """List active obligations, optionally filtered by status."""
    if ctx.db is None:
        return {"error": "no database configured"}

    from sqlmodel import select

    from agent_core.state.models import Obligation, ObligationStatus

    status_filter = args.get("status")
    limit = args.get("limit", 20)

    with ctx.db.session() as s:
        stmt = select(Obligation).order_by(Obligation.priority.desc(), Obligation.created_at.desc())
        if status_filter:
            try:
                target = ObligationStatus(status_filter)
                stmt = stmt.where(Obligation.status == target)
            except ValueError:
                return {"error": f"unknown status: {status_filter!r}"}
        else:
            stmt = stmt.where(Obligation.status != ObligationStatus.done)
        rows = list(s.exec(stmt.limit(limit)).all())

    return {
        "count": len(rows),
        "obligations": [
            {
                "id": ob.id[:8],
                "title": ob.title,
                "status": ob.status.value,
                "priority": ob.priority,
                "source": ob.source.value if ob.source else None,
            }
            for ob in rows
        ],
    }


def _search_memory_handler(args: dict, ctx: ToolContext) -> dict:
    """Semantic search across captured thoughts (chat history, vault, emails)."""
    if ctx.openbrain is None:
        return {"error": "openbrain not configured"}
    query = args.get("query") or ""
    limit = args.get("limit", 3)
    if not query:
        return {"error": "query is required"}
    try:
        hits = ctx.openbrain.search(query, limit=limit)
    except Exception as e:
        return {"error": f"search failed: {e}"}

    return {
        "count": len(hits),
        "hits": [
            {
                "content": (getattr(h.thought, "content", "") or "")[:300],
                "similarity": round(getattr(h, "similarity", 0), 3),
                "source_kind": (h.sources[0].source_kind if getattr(h, "sources", None) else None),
            }
            for h in hits
        ],
    }


def _today_calendar_handler(args: dict, ctx: ToolContext) -> dict:
    """Return today's calendar events (UTC midnight to next-midnight)."""
    if ctx.calendar is None:
        return {"error": "calendar not configured"}
    try:
        from agent_core.work.calendar import fetch_today

        events = fetch_today(ctx.calendar)
    except Exception as e:
        return {"error": f"fetch failed: {e}"}
    return {"count": len(events), "events": [_event_to_dict(e) for e in events]}


def _upcoming_calendar_handler(args: dict, ctx: ToolContext) -> dict:
    """Return events in the next ``hours`` from now."""
    if ctx.calendar is None:
        return {"error": "calendar not configured"}
    hours = args.get("hours", 24)
    try:
        from agent_core.work.calendar import fetch_window

        events = fetch_window(ctx.calendar, hours=hours)
    except Exception as e:
        return {"error": f"fetch failed: {e}"}
    return {"count": len(events), "events": [_event_to_dict(e) for e in events]}


def _event_to_dict(ev: Any) -> dict:
    return {
        "summary": ev.summary,
        "start": ev.start.isoformat() if ev.start else None,
        "end": ev.end.isoformat() if ev.end else None,
        "all_day": ev.all_day,
        "location": ev.location,
    }


_DEFAULT_READ_TOOLS = [
    ToolDefinition(
        name="list_obligations",
        description=(
            "List the user's active obligations (tasks they're tracking). "
            "Use this when the user asks about their tasks, to-do list, "
            "what's on their plate, what they should work on, etc."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": (
                        "Optional filter by status: inbox, in-progress, "
                        "waiting, blocked, done. Omit for all-active."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max obligations to return (default 20).",
                    "default": 20,
                },
            },
        },
        handler=_list_obligations_handler,
    ),
    ToolDefinition(
        name="search_memory",
        description=(
            "Semantic search across the user's captured thoughts: prior "
            "chat conversations, vault notes, emails, anything in OpenBrain. "
            "Use when the user asks 'what did we say about X' or 'do I have "
            "anything related to Y'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query to search for.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max hits to return (default 3).",
                    "default": 3,
                },
            },
            "required": ["query"],
        },
        handler=_search_memory_handler,
    ),
    ToolDefinition(
        name="today_calendar",
        description=(
            "Return today's calendar events. Use when the user asks about "
            "today's schedule, meetings, what's on the calendar."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_today_calendar_handler,
    ),
    ToolDefinition(
        name="upcoming_calendar",
        description=(
            "Return upcoming calendar events in the next N hours. Use for "
            "'what's coming up', 'next meeting', 'this week's schedule'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Hours ahead to look. 24 = next day, 168 = next week.",
                    "default": 24,
                },
            },
        },
        handler=_upcoming_calendar_handler,
    ),
]


def default_read_tools() -> list[ToolDefinition]:
    """Return the curated read-only tool set: list_obligations,
    search_memory, today_calendar, upcoming_calendar.

    Returned by value — callers can append / filter without affecting
    the canonical list.
    """
    return list(_DEFAULT_READ_TOOLS)


# ── Tool execution ─────────────────────────────────────────────────────────


def execute_tool_call(
    call: ToolCall,
    tools: list[ToolDefinition],
    context: ToolContext,
) -> str:
    """Dispatch one tool call to its handler, return JSON-stringified result.

    Handler exceptions are caught and returned as ``{"error": ...}`` so
    the LLM sees a structured failure rather than the loop crashing.
    """
    tool = next((t for t in tools if t.name == call.name), None)
    if tool is None:
        return json.dumps({"error": f"unknown tool: {call.name!r}"})
    try:
        result = tool.handler(call.arguments, context)
    except Exception as e:
        logger.exception("tool %s raised", call.name)
        result = {"error": f"{type(e).__name__}: {e}"}
    if not isinstance(result, str):
        result = json.dumps(result, default=str)
    return result


# ── Tool loop ──────────────────────────────────────────────────────────────


DEFAULT_MAX_ITERATIONS = 5


def run_tool_loop(
    *,
    language_model: Any,
    system: str,
    user_message: str,
    history: list[dict],
    tools: list[ToolDefinition],
    context: ToolContext,
    max_tokens: int = 2048,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    on_tool_call: Callable[[str, dict], None] | None = None,
) -> str:
    """Run a chat turn with tool-use until the LLM produces text.

    The loop:
        1. Send (system, history + user, tools) to the LLM.
        2. If the response has tool_calls, execute each, append the
           results to the message list as ``{role: "tool", ...}``,
           then go to step 1.
        3. If the response has content (and no tool_calls), return it.
        4. After ``max_iterations``, return the last content (or a
           safe message) without further calls.

    ``history`` is a list of OpenAI-format messages
    (``{role, content}``). The user_message is appended internally. The
    function does NOT mutate ``history`` — caller owns persistence.

    ``on_tool_call`` is an optional callback for the chat REPL to print
    "🔧 calling list_obligations…" status lines.

    Falls back to plain ``language_model.complete()`` when the LM
    doesn't implement ``complete_with_tools()`` (e.g., stub LMs in
    tests that use the plain interface).
    """
    if not hasattr(language_model, "complete_with_tools"):
        # Fallback: no tool support, do a straight completion
        return language_model.complete(system=system, user=user_message, max_tokens=max_tokens)

    messages: list[dict] = list(history)
    messages.append({"role": "user", "content": user_message})

    last_content: str | None = None
    for _iteration in range(max_iterations):
        try:
            resp: CompletionResponse = language_model.complete_with_tools(
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logger.exception("complete_with_tools failed")
            return f"(LLM call failed: {e})"

        if resp.content is not None:
            last_content = resp.content

        if not resp.tool_calls:
            return resp.content or last_content or ""

        # Append the assistant's tool-call request, then each tool result.
        # OpenAI requires the assistant message contain the tool_calls
        # array, then a follow-up `tool` message per call.
        messages.append(
            {
                "role": "assistant",
                "content": resp.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in resp.tool_calls
                ],
            }
        )
        for tc in resp.tool_calls:
            if on_tool_call:
                import contextlib

                with contextlib.suppress(Exception):
                    on_tool_call(tc.name, tc.arguments)
            result = execute_tool_call(tc, tools, context)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": result,
                }
            )

    # Loop exhausted
    if last_content:
        return last_content
    return "(tool loop exceeded max iterations without producing a final answer)"


__all__ = [
    "CompletionResponse",
    "DEFAULT_MAX_ITERATIONS",
    "ToolCall",
    "ToolContext",
    "ToolDefinition",
    "default_read_tools",
    "execute_tool_call",
    "run_tool_loop",
]
