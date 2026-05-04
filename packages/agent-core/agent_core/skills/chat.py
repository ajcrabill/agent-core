"""Direct chat with the configured LLM, with agent-core context injected.

This is the "talk to your agent" surface that doesn't require OpenWebUI,
Node, or any UI dependency. It's a thin REPL on top of the same
``LanguageModel`` Protocol the skills use.

Why a chat module (not just a CLI command): we want the same
context-injection logic available to:
    - the CLI REPL (``dcos chat``)
    - the ``/chat`` HTTP endpoint (Sprint 15c)
    - downstream skill packages that want to start a sub-conversation

So the loop logic (build context, call LM, manage history) lives here.
The CLI is just a thin wrapper that hooks stdin/stdout to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Message + Session ──────────────────────────────────────────────────────


@dataclass
class ChatMessage:
    """One turn in a conversation."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatSession:
    """A conversation. Holds the message history + context-injection settings.

    Cross-session memory comes from auto-capturing every turn into
    OpenBrain. Future turns then surface relevant prior conversations
    via the same context-injection that surfaces vault notes / emails.
    The session itself stays in-memory; OpenBrain is the durable layer.
    """

    history: list[ChatMessage] = field(default_factory=list)
    system_prompt: str = ""
    inject_obligations: bool = True
    """Pull active obligations into the system prompt before each turn."""
    inject_openbrain: bool = True
    """Run a semantic search against the user's last message and include hits."""
    openbrain_hits: int = 3
    """How many openbrain hits to include per turn."""
    obligation_limit: int = 10
    """How many active obligations to include."""
    record_to_openbrain: bool = True
    """If True (default), each completed turn captures the (user, agent)
    exchange as a Thought with source_kind='chat'. Enables the agent to
    remember conversations across sessions — search('what did we say
    about X?') will surface prior chats."""
    session_id: str | None = None
    """Stable identifier for the conversation. Set when the chat starts
    (CLI: per-process; HTTP: per-session). Used as the source_uri for
    captured Thoughts so they're grouped + browsable later."""

    def append(self, role: str, content: str) -> None:
        self.history.append(ChatMessage(role=role, content=content))

    def reset(self) -> None:
        """Drop the message history. Keeps system_prompt and toggles."""
        self.history.clear()


# ── Default system prompt ──────────────────────────────────────────────────


DEFAULT_SYSTEM_PROMPT = """You are a digital chief of staff for the user. \
You have access to their obligation board (active tasks they're tracking) \
and their semantic memory (notes, emails, prior conversations). When the \
user asks a question, ground your answer in that context. When you're \
unsure, say so. Be concise — bullet points beat prose."""


# ── Context injection ──────────────────────────────────────────────────────


def build_context_prompt(
    *,
    base_system: str,
    obligations: list | None = None,
    openbrain_hits: list | None = None,
) -> str:
    """Compose the system prompt: base + obligations + openbrain hits.

    Each section gets a clearly-labeled header so the model can refer back
    to citations explicitly. Empty sections are dropped (not included as
    "no current obligations" etc. — saves tokens).
    """
    parts: list[str] = [base_system]

    if obligations:
        lines = ["", "## Currently active obligations"]
        for i, ob in enumerate(obligations, start=1):
            status = getattr(ob, "status", None)
            status_str = status.value if hasattr(status, "value") else str(status)
            title = getattr(ob, "title", "(untitled)")
            body = getattr(ob, "body", None)
            line = f"{i}. [{status_str}] {title}"
            if body:
                # Trim long bodies — full text isn't always useful as context
                snippet = body[:200].replace("\n", " ")
                line += f" — {snippet}"
            lines.append(line)
        parts.append("\n".join(lines))

    if openbrain_hits:
        lines = ["", "## Relevant context from your semantic memory"]
        for i, hit in enumerate(openbrain_hits, start=1):
            content = getattr(hit.thought, "content", "")[:300]
            sim = round(getattr(hit, "similarity", 0), 3)
            sources = getattr(hit, "sources", [])
            src_str = (
                f" (source: {sources[0].source_kind})" if sources else ""
            )
            lines.append(f"[{i}]{src_str} (similarity={sim}) {content}")
        parts.append("\n".join(lines))

    return "\n".join(parts)


# ── Single-turn execution ──────────────────────────────────────────────────


def run_turn(
    *,
    user_message: str,
    session: ChatSession,
    language_model: Any,
    db: Any | None = None,
    openbrain: Any | None = None,
    max_tokens: int = 2048,
) -> str:
    """Run one chat turn. Returns the assistant's reply, mutates ``session``.

    Pipeline:
      1. Look up active obligations (if inject_obligations + db given).
      2. Search openbrain for context (if inject_openbrain + openbrain given).
      3. Build context-injected system prompt.
      4. Call the LM with [system, ...history, user].
      5. Append user + assistant messages to history.
    """
    obligations = []
    if session.inject_obligations and db is not None:
        obligations = _fetch_active_obligations(db, limit=session.obligation_limit)

    hits = []
    if session.inject_openbrain and openbrain is not None:
        try:
            hits = openbrain.search(user_message, limit=session.openbrain_hits)
        except Exception as e:
            logger.debug("openbrain search failed: %s", e)
            hits = []

    system = build_context_prompt(
        base_system=session.system_prompt or DEFAULT_SYSTEM_PROMPT,
        obligations=obligations,
        openbrain_hits=hits,
    )

    # Compose the LM request: prior history flattened to a single user
    # message string. (The LanguageModel Protocol takes system+user only;
    # multi-turn history needs to be concatenated. Future Protocol version
    # will accept a list of messages.)
    if session.history:
        history_str = "\n\n".join(
            f"{m.role.title()}: {m.content}" for m in session.history
        )
        user = f"{history_str}\n\nUser: {user_message}"
    else:
        user = user_message

    reply = language_model.complete(
        system=system,
        user=user,
        max_tokens=max_tokens,
    )

    session.append("user", user_message)
    session.append("assistant", reply)

    # Cross-session memory: capture this turn into OpenBrain so future
    # chats can recall it via semantic search. Failure here MUST NOT
    # break the chat turn — log + swallow.
    if session.record_to_openbrain and openbrain is not None:
        try:
            content = f"User: {user_message}\n\nAgent: {reply}"
            openbrain.capture(
                content,
                source_kind="chat",
                source_uri=session.session_id,
                source_title="chat turn",
                metadata={
                    "session_id": session.session_id,
                    "history_length_after": len(session.history),
                },
            )
        except Exception as e:
            logger.debug("chat-memory capture failed: %s", e)

    return reply


def _fetch_active_obligations(db: Any, *, limit: int = 10) -> list:
    """Return up to ``limit`` non-done obligations, priority-ordered."""
    from sqlmodel import select

    from agent_core.state.models import Obligation, ObligationStatus

    with db.session() as s:
        stmt = (
            select(Obligation)
            .where(Obligation.status != ObligationStatus.done)
            .order_by(Obligation.priority.desc(), Obligation.created_at.desc())
            .limit(limit)
        )
        return list(s.exec(stmt).all())


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "ChatMessage",
    "ChatSession",
    "build_context_prompt",
    "run_turn",
]
