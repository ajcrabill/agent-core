"""Browser chat endpoint — talk to the agent without OpenWebUI.

Two routes mounted under ``/chat``:

  - ``GET /chat`` returns a single-file HTML page (vanilla HTML + CSS +
    JS, no build step, no npm dependency). Renders a chat box that POSTs
    to /chat/turn.

  - ``POST /chat/turn`` takes ``{message, session_id, history}`` and
    returns ``{reply, session_id, history}``. Uses the configured
    LanguageModel + injects active obligations + openbrain hits per-turn.

This is the lightweight path that proves "you have an AI assistant" the
moment serve is up — no Node, no OpenWebUI, no build pipeline. Graduate
to OpenWebUI for a richer UI when ready.

Sessions are in-memory only. A page reload starts a new conversation.
The persistent-conversation backend is OpenBrain (anything you want
remembered, capture explicitly).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agent_core.skills import (
    DEFAULT_SYSTEM_PROMPT,
    ChatSession,
    LanguageModelError,
    language_model_from_settings,
    run_turn,
)
from agent_core.web.auth import require_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# ── Schemas ────────────────────────────────────────────────────────────────


class ChatTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)
    session_id: str | None = Field(
        default=None,
        description="Pass back the session_id from a prior response to continue a conversation.",
    )
    history: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Conversation so far. Each item is {role: 'user'|'assistant', "
            "content: str}. The browser holds history client-side; the server "
            "is stateless across requests."
        ),
    )
    inject_context: bool = Field(
        default=True,
        description="Inject obligations + openbrain hits into the system prompt.",
    )


class ChatTurnResponse(BaseModel):
    reply: str
    session_id: str
    history: list[dict[str, str]]


# ── HTML page ──────────────────────────────────────────────────────────────


_CHAT_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>chat — agent-core</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #f9fafb;
    --fg: #111827;
    --muted: #6b7280;
    --accent: #3b82f6;
    --user-bg: #dbeafe;
    --agent-bg: #f3f4f6;
    --border: #e5e7eb;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0b0f17;
      --fg: #e5e7eb;
      --muted: #9ca3af;
      --accent: #60a5fa;
      --user-bg: #1e3a8a;
      --agent-bg: #1f2937;
      --border: #374151;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  header { padding: 12px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
    background: var(--bg); position: sticky; top: 0; z-index: 10; }
  header h1 { font-size: 14px; font-weight: 600; margin: 0; }
  header small { color: var(--muted); font-size: 12px; }
  #conversation { max-width: 760px; margin: 0 auto; padding: 24px 20px 140px; }
  .turn { margin-bottom: 16px; }
  .turn .role { font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--muted); margin-bottom: 4px; }
  .turn .body { padding: 12px 14px; border-radius: 10px;
    white-space: pre-wrap; word-wrap: break-word; }
  .turn.user .body { background: var(--user-bg); }
  .turn.agent .body { background: var(--agent-bg); border: 1px solid var(--border); }
  .turn.error .body { background: #fef2f2; color: #991b1b;
    border: 1px solid #fecaca; }
  @media (prefers-color-scheme: dark) {
    .turn.error .body { background: #450a0a; color: #fca5a5; border-color: #7f1d1d; }
  }
  #composer-wrap { position: fixed; bottom: 0; left: 0; right: 0;
    background: var(--bg); border-top: 1px solid var(--border);
    padding: 12px 20px 20px; }
  #composer { max-width: 760px; margin: 0 auto; display: flex; gap: 8px; }
  #message { flex: 1; padding: 10px 12px; border: 1px solid var(--border);
    border-radius: 10px; background: var(--bg); color: var(--fg);
    font-family: inherit; font-size: 15px; resize: none; min-height: 44px;
    max-height: 200px; }
  button { padding: 10px 18px; border: 0; border-radius: 10px;
    background: var(--accent); color: white; font-weight: 600;
    cursor: pointer; font-size: 14px; }
  button:disabled { opacity: 0.5; cursor: wait; }
  #auth { padding: 12px 20px; background: #fef3c7; color: #78350f;
    border-bottom: 1px solid #fcd34d; font-size: 13px; }
  @media (prefers-color-scheme: dark) {
    #auth { background: #422006; color: #fcd34d; border-color: #92400e; }
  }
  #auth.hidden { display: none; }
  #auth input { padding: 6px 10px; border: 1px solid #fcd34d;
    border-radius: 6px; font-family: inherit; }
  .pending { color: var(--muted); font-style: italic; }
</style>
</head>
<body>
<header>
  <h1>agent-core / chat</h1>
  <small id="provider">loading…</small>
</header>
<div id="auth">
  Paste your bearer token (from <code>dcos init</code>):
  <input type="password" id="token" size="48" placeholder="…">
  <button onclick="saveToken()">Save</button>
</div>
<div id="conversation">
  <div class="turn agent"><div class="role">agent</div>
    <div class="body">Hi — what can I help you with?

I have access to your active obligations and your semantic memory. Ask me about your tasks, or anything you've captured.</div></div>
</div>
<div id="composer-wrap">
  <form id="composer">
    <textarea id="message" placeholder="Message your agent…" rows="1" autofocus></textarea>
    <button type="submit" id="send">Send</button>
  </form>
</div>
<script>
const TOKEN_KEY = "agent-core-chat-token";
let token = localStorage.getItem(TOKEN_KEY) || "";
let history = [];
let session_id = null;

function el(s) { return document.querySelector(s); }
function showAuth(show) { el("#auth").classList.toggle("hidden", !show); }
function saveToken() {
  const t = el("#token").value.trim();
  if (!t) return;
  token = t;
  localStorage.setItem(TOKEN_KEY, t);
  showAuth(false);
  el("#provider").textContent = "ready";
}

if (token) showAuth(false);

el("#message").addEventListener("input", e => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(200, e.target.scrollHeight) + "px";
});

el("#message").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    el("#composer").requestSubmit();
  }
});

el("#composer").addEventListener("submit", async e => {
  e.preventDefault();
  if (!token) { showAuth(true); return; }
  const msg = el("#message").value.trim();
  if (!msg) return;
  el("#message").value = "";
  el("#message").style.height = "auto";
  appendTurn("user", msg);
  const pending = appendTurn("agent", "thinking…", "pending");
  el("#send").disabled = true;
  try {
    const resp = await fetch("/chat/turn", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
      },
      body: JSON.stringify({ message: msg, session_id, history }),
    });
    if (resp.status === 401) {
      showAuth(true);
      pending.remove();
      el("#send").disabled = false;
      return;
    }
    if (!resp.ok) {
      const text = await resp.text();
      pending.querySelector(".body").textContent = "Error: " + text;
      pending.classList.remove("pending");
      pending.classList.add("error");
      pending.classList.remove("agent");
      el("#send").disabled = false;
      return;
    }
    const data = await resp.json();
    pending.querySelector(".body").textContent = data.reply;
    pending.classList.remove("pending");
    history = data.history;
    session_id = data.session_id;
  } catch (err) {
    pending.querySelector(".body").textContent = "Network error: " + err.message;
    pending.classList.remove("pending");
    pending.classList.add("error");
    pending.classList.remove("agent");
  } finally {
    el("#send").disabled = false;
    el("#message").focus();
  }
});

function appendTurn(role, text, extra) {
  const div = document.createElement("div");
  div.className = "turn " + role + (extra ? " " + extra : "");
  div.innerHTML = '<div class="role">' + role + '</div><div class="body"></div>';
  div.querySelector(".body").textContent = text;
  el("#conversation").appendChild(div);
  div.scrollIntoView({ behavior: "smooth", block: "end" });
  return div;
}

el("#provider").textContent = token ? "ready" : "needs token";
</script>
</body>
</html>
"""


# ── Routes ─────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
def chat_page() -> HTMLResponse:
    """Vanilla HTML chat page. No auth required to GET — the page itself
    is static. The XHR call to /chat/turn requires a bearer token."""
    return HTMLResponse(_CHAT_HTML)


# In-memory session store. Keys are session_ids; values are ChatSession.
# Cleared on server restart; that's fine — chat is stateless across reloads
# anyway (history is held by the browser).
_SESSIONS: dict[str, ChatSession] = {}


@router.post("/turn", response_model=ChatTurnResponse, dependencies=[Depends(require_token)])
def chat_turn(request: Request, body: ChatTurnRequest) -> ChatTurnResponse:
    """Run one chat turn. Stateless across requests — the browser holds
    history; the server reconstitutes a ChatSession from it per call.
    """
    from agent_core.secrets import default_store

    settings = request.app.state.settings
    settings_obj = getattr(settings, "settings", settings)  # SettingsManager → AgentSettings

    # Build the LanguageModel
    try:
        lm = language_model_from_settings(settings_obj, default_store())
    except LanguageModelError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"LLM not configured: {e}. Run "
                "`dcos init --llm-provider openai_compat --llm-api-key sk-...`"
            ),
        ) from e

    db = getattr(request.app.state, "db", None)
    openbrain = getattr(request.app.state, "openbrain", None)

    # Reconstitute session from request
    session_id = body.session_id or str(uuid.uuid4())
    session = ChatSession(
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        inject_obligations=body.inject_context and db is not None,
        inject_openbrain=body.inject_context and openbrain is not None,
        session_id=f"web-{session_id}",
    )
    for msg in body.history:
        session.append(msg.get("role", "user"), msg.get("content", ""))

    try:
        reply = run_turn(
            user_message=body.message,
            session=session,
            language_model=lm,
            db=db,
            openbrain=openbrain,
        )
    except LanguageModelError as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}") from e

    return ChatTurnResponse(
        reply=reply,
        session_id=session_id,
        history=[{"role": m.role, "content": m.content} for m in session.history],
    )


__all__ = ["router"]
