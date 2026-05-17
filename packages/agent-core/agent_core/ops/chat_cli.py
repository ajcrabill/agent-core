"""Shared `chat`, `remember`, and `recall` CLI commands.

Mounted via add_command in both dcos-agent and ikb-agent. Backed by
agent_core.skills (chat REPL + tools) and agent_core.openbrain (semantic
memory).

The chat REPL ships with slash-command shortcuts that drive the same
agent surfaces as the standalone CLI commands — /run, /digest, /triage,
/capture, /drafts, /send, /today. Helpers for each live in this module
so both products share the implementation.
"""

from __future__ import annotations

import sys as _sys
import uuid as _uuid
from pathlib import Path

import click
from rich.console import Console

console = Console()


# ── remember ──────────────────────────────────────────────────────────────


@click.command(name="remember")
@click.argument("content", nargs=-1)
@click.option(
    "--source-kind",
    default="manual",
    show_default=True,
    help="Provenance hint for filtering / dashboards later.",
)
@click.option(
    "--source-uri",
    default=None,
    help="Where this came from — URL, file path, message ID, etc.",
)
@click.option(
    "--source-title",
    default=None,
    help="Human-readable title (e.g., subject line, doc heading).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
)
@click.option(
    "--db-url",
    default=None,
)
@click.option(
    "--from-stdin",
    is_flag=True,
    help="Read content from stdin instead of CLI args. Useful for piping.",
)
def remember_command(
    content,
    source_kind,
    source_uri,
    source_title,
    config_path,
    db_url,
    from_stdin,
):
    """Quick-capture a thought into OpenBrain.

    The agent will surface this in future chats whose user message is
    semantically related. Three input modes:

    \b
        <product> remember "Robyne prefers Tuesday meetings"
        echo "long content..." | <product> remember --from-stdin
        <product> remember "SMS from Charlotte" --source-kind sms --source-uri 555-1234
    """
    from agent_core.openbrain import OpenBrainStore
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database

    text = _sys.stdin.read().strip() if from_stdin else " ".join(content).strip()

    if not text:
        console.print("[red]nothing to remember:[/red] pass content as args or --from-stdin")
        raise click.exceptions.Exit(2)

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    resolved_url = db_url or mgr.get("storage.url")
    if not resolved_url:
        console.print("[red]no db url:[/red] set storage.url or pass --db-url")
        raise click.exceptions.Exit(1)
    db = Database(resolved_url)
    store = OpenBrainStore.from_settings(mgr.settings, db)

    thought = store.capture(
        text,
        source_kind=source_kind,
        source_uri=source_uri,
        source_title=source_title,
    )
    console.print(f"[green]captured[/green] id={thought.id[:8]}…")
    console.print(
        f"[dim]source_kind={source_kind}{' uri=' + source_uri if source_uri else ''}[/dim]"
    )
    if len(text) > 100:
        console.print(f"[dim]content: {text[:100]}…[/dim]")
    else:
        console.print(f"[dim]content: {text}[/dim]")


# ── recall ────────────────────────────────────────────────────────────────


@click.command(name="recall")
@click.argument("query", nargs=-1, required=True)
@click.option("--limit", default=5, type=int, show_default=True)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
)
@click.option(
    "--db-url",
    default=None,
)
def recall_command(query, limit, config_path, db_url):
    """Semantic search across captured thoughts.

    Pairs with `<product> remember`. Hits include similarity scores +
    source provenance so you can trace each result.

    \b
        <product> remember "Robyne prefers Tuesday meetings"
        <product> recall meetings with Robyne
        # → finds the earlier capture
    """
    from agent_core.openbrain import OpenBrainStore
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database

    text = " ".join(query).strip()
    if not text:
        console.print("[red]empty query[/red]")
        raise click.exceptions.Exit(2)

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    resolved_url = db_url or mgr.get("storage.url")
    if not resolved_url:
        console.print("[red]no db url:[/red] set storage.url or pass --db-url")
        raise click.exceptions.Exit(1)
    db = Database(resolved_url)
    store = OpenBrainStore.from_settings(mgr.settings, db)

    hits = store.search(text, limit=limit)
    if not hits:
        console.print("[dim]no hits[/dim]")
        return

    for i, h in enumerate(hits, start=1):
        sim = round(h.similarity, 3)
        src = h.sources[0] if h.sources else None
        src_str = f" ({src.source_kind})" if src else ""
        console.print(f"[bold cyan]{i}.[/bold cyan] [dim]similarity={sim}{src_str}[/dim]")
        snippet = h.thought.content[:300].replace("\n", " ")
        console.print(f"   {snippet}")
        console.print()


# ── chat ───────────────────────────────────────────────────────────────────


@click.command(name="chat")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
)
@click.option(
    "--db-url",
    default=None,
)
@click.option(
    "--no-context",
    is_flag=True,
    help="Don't inject obligations + openbrain hits + calendar into the system prompt.",
)
@click.option(
    "--system",
    "system_prompt",
    default=None,
    help="Override the default system prompt.",
)
@click.option(
    "--max-tokens",
    default=2048,
    type=int,
    show_default=True,
    help="Per-turn ceiling for the model's output length.",
)
@click.option(
    "--stub-llm",
    is_flag=True,
    help="Force the smart stub even when a real LLM is configured.",
)
def chat_command(config_path, db_url, no_context, system_prompt, max_tokens, stub_llm):
    """Talk to your agent in a CLI REPL.

    Loads the configured LLM, opens a conversation, and injects active
    obligations + relevant openbrain hits + today's calendar into each
    turn's system prompt by default.

    The model can also call read-only tools mid-turn (list_obligations,
    search_memory, today_calendar, upcoming_calendar) — falls back to
    plain completion if your model doesn't support tool-use.

    Slash commands inside chat:

    \b
      /help, /reset, /context
      /triage, /run, /digest [hrs], /capture <text>
      /drafts, /send <id>
      /today
      /exit
    """
    from agent_core.openbrain import OpenBrainStore
    from agent_core.secrets import default_store
    from agent_core.settings import SettingsManager
    from agent_core.skills import (
        DEFAULT_SYSTEM_PROMPT,
        ChatSession,
        LanguageModelError,
        language_model_from_settings,
        run_turn,
    )
    from agent_core.state.db import Database

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    resolved_url = db_url or mgr.get("storage.url")
    db = Database(resolved_url) if resolved_url else None
    openbrain = OpenBrainStore.from_settings(mgr.settings, db) if db else None

    # Calendar: optional, opt-in via settings. Built once per session.
    calendar = None
    if not no_context and mgr.settings.calendar.enabled and mgr.settings.calendar.inject_into_chat:
        from agent_core.work.calendar import CalendarFetcher, CalendarFetchError

        try:
            calendar = CalendarFetcher.from_settings(mgr.settings, default_store())
        except CalendarFetchError as e:
            console.print(f"[yellow]calendar disabled:[/yellow] {e}")

    if stub_llm:
        lm = _smart_stub_lm()
        provider_label = "stub-llm (forced)"
    else:
        try:
            lm = language_model_from_settings(mgr.settings, default_store())
            provider_label = f"{mgr.settings.llm.provider} / {mgr.settings.llm.model}"
        except LanguageModelError as e:
            console.print(f"[red]LLM not configured:[/red] {e}")
            console.print(
                "[dim]Run [cyan]<product> init --llm-provider openai_compat "
                "--llm-api-key sk-...[/cyan] (or --llm-provider ollama for "
                "local), or use [cyan]--stub-llm[/cyan].[/dim]"
            )
            raise click.exceptions.Exit(1) from e

    session = ChatSession(
        system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
        inject_obligations=not no_context and db is not None,
        inject_openbrain=not no_context and openbrain is not None,
        session_id=f"cli-{_uuid.uuid4()}",
    )

    console.print(f"[dim]chatting with {provider_label}. Ctrl-D or /exit to quit.[/dim]")
    console.print(
        "[dim]Slash commands: /help, /reset, /context, /triage, /run, /digest, "
        "/capture, /drafts, /send, /today, /exit.[/dim]"
    )
    console.print()

    while True:
        try:
            line = click.prompt("you", prompt_suffix="> ", default="", show_default=False)
        except (EOFError, click.exceptions.Abort):
            console.print("\n[dim]bye[/dim]")
            break
        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            console.print("[dim]bye[/dim]")
            break
        if text in ("/help", "/?"):
            console.print(
                "[dim]"
                "/help          show this list\n"
                "/reset         clear chat history (keeps system prompt)\n"
                "/context       toggle obligation + openbrain injection\n"
                "/triage        run inbox auto-triage now\n"
                "/run           run a single autonomous tick (stalled detection + triage)\n"
                "/digest [hrs]  show recent agent activity (default 24h)\n"
                "/capture <text> add an inbox obligation; next /triage will classify it\n"
                "/drafts        list pending email drafts awaiting your approval\n"
                "/send <id>     send a drafted reply via SMTP (with preview)\n"
                "/today         show today's calendar (if calendar.enabled)\n"
                "/exit          quit"
                "[/dim]"
            )
            continue
        if text == "/reset":
            session.reset()
            console.print("[dim]history cleared[/dim]")
            continue
        if text == "/context":
            session.inject_obligations = not session.inject_obligations
            session.inject_openbrain = not session.inject_openbrain
            state = "ON" if session.inject_obligations else "OFF"
            console.print(f"[dim]context injection: {state}[/dim]")
            continue
        if text == "/triage":
            _run_triage_inline(db=db, settings=mgr, language_model=lm)
            continue
        if text == "/run":
            _run_tick_inline(db=db, settings=mgr, language_model=lm)
            continue
        if text.startswith("/digest"):
            parts = text.split(maxsplit=1)
            try:
                hours = float(parts[1]) if len(parts) > 1 else 24.0
            except ValueError:
                console.print(
                    f"[yellow]/digest expects a number of hours, got {parts[1]!r}[/yellow]"
                )
                continue
            _show_digest_inline(db=db, hours=hours)
            continue
        if text.startswith("/capture"):
            payload = text[len("/capture") :].strip()
            if not payload:
                console.print(
                    "[yellow]usage: /capture Email from x@y.com: subject\\nbody…[/yellow]"
                )
                continue
            ob_id = _capture_inline(db=db, raw=payload)
            console.print(f"[dim]captured obligation {ob_id[:8]} — try /triage to classify[/dim]")
            continue
        if text == "/drafts":
            _list_drafts_inline(db=db)
            continue
        if text == "/today":
            if calendar is None:
                console.print(
                    "[yellow]calendar not configured. "
                    "Set calendar.enabled=true and stash the ICS URL.[/yellow]"
                )
            else:
                from agent_core.ops.calendar_cli import _print_calendar_events
                from agent_core.work.calendar import fetch_today

                events = fetch_today(calendar)
                _print_calendar_events(events)
            continue
        if text.startswith("/send"):
            target = text[len("/send") :].strip()
            if not target:
                console.print("[yellow]usage: /send <obligation-id-prefix>[/yellow]")
                continue
            _send_draft_inline(db=db, settings=mgr, target=target)
            continue
        if text.startswith("/"):
            console.print(f"[yellow]unknown command:[/yellow] {text}")
            continue

        def _on_tool_call(tool_name: str, args: dict) -> None:
            arg_preview = ", ".join(f"{k}={v!r}" for k, v in args.items())[:60]
            console.print(f"[dim]🔧 {tool_name}({arg_preview})[/dim]")

        try:
            reply = run_turn(
                user_message=text,
                session=session,
                language_model=lm,
                db=db,
                openbrain=openbrain,
                calendar=calendar,
                max_tokens=max_tokens,
                on_tool_call=_on_tool_call,
            )
        except LanguageModelError as e:
            console.print(f"[red]LLM error:[/red] {e}")
            continue
        except KeyboardInterrupt:
            console.print("\n[dim](interrupted)[/dim]")
            continue

        console.print(f"[bold cyan]agent:[/bold cyan] {reply}")
        console.print()


# ── Inline helpers (used by chat slash commands) ───────────────────────────


def _run_triage_inline(*, db, settings, language_model) -> None:
    """Run triage_inbox and print a one-line summary to the chat console."""
    import agent_core.skills  # noqa: F401  ensures email-triage is registered
    from agent_core.agent.run_loop import triage_inbox

    if db is None:
        console.print("[yellow]/triage requires a database; aborting.[/yellow]")
        return
    report = triage_inbox(db=db, settings=settings, language_model=language_model)
    if report.errors:
        for err in report.errors:
            console.print(f"[red]triage error:[/red] {err}")
    by_action = ", ".join(f"{n} {a}" for a, n in sorted(report.by_action.items())) or "none"
    console.print(
        f"[dim]triage: {report.candidates} candidates, {report.triaged} classified "
        f"({by_action}), {report.skipped_already_triaged} already triaged.[/dim]"
    )


def _run_tick_inline(*, db, settings, language_model) -> None:
    """Run a single autonomous tick (run_tick) and print summary."""
    import agent_core.skills  # noqa: F401
    from agent_core.agent.run_loop import run_tick
    from agent_core.notifications import NotificationDispatcher

    if db is None:
        console.print("[yellow]/run requires a database; aborting.[/yellow]")
        return
    try:
        dispatcher = NotificationDispatcher.from_settings(settings.settings)
    except Exception:
        dispatcher = None
    report = run_tick(
        db=db,
        settings=settings,
        dispatcher=dispatcher,
        language_model=language_model,
    )
    triage = report.triage
    triage_str = (
        f"triage: {triage.candidates} candidates, {triage.triaged} classified."
        if triage is not None
        else "triage: skipped"
    )
    console.print(
        f"[dim]tick: {report.stalled_total} stalled, "
        f"{report.incidents_created} new incidents, "
        f"{report.notifications_sent} notifications sent. "
        f"{triage_str}[/dim]"
    )
    for err in report.errors:
        console.print(f"[red]tick error:[/red] {err}")


def _show_digest_inline(*, db, hours: float) -> None:
    from agent_core.actions.digest import DailyDigestBuilder

    if db is None:
        console.print("[yellow]/digest requires a database; aborting.[/yellow]")
        return
    digest = DailyDigestBuilder(db, period_hours=hours).build()
    console.print(digest.as_markdown())


def _list_drafts_inline(*, db) -> None:
    """List pending email drafts to the chat console."""
    from sqlmodel import select

    from agent_core.state.models import (
        Obligation,
        ObligationEvent,
        ObligationEventKind,
        ObligationSource,
        ObligationStatus,
    )

    if db is None:
        console.print("[yellow]/drafts requires a database; aborting.[/yellow]")
        return

    with db.session() as s:
        obs = list(
            s.exec(
                select(Obligation)
                .where(Obligation.source == ObligationSource.inbound_email)
                .where(Obligation.status == ObligationStatus.in_progress)
            ).all()
        )
        ob_ids = [ob.id for ob in obs]
        events = (
            list(
                s.exec(
                    select(ObligationEvent).where(
                        ObligationEvent.obligation_id.in_(ob_ids),
                        ObligationEvent.kind == ObligationEventKind.comment,
                    )
                ).all()
            )
            if ob_ids
            else []
        )

    by_ob: dict[str, list] = {}
    for ev in events:
        by_ob.setdefault(ev.obligation_id, []).append(ev)

    pending: list[tuple[str, dict]] = []
    for ob in obs:
        evs = by_ob.get(ob.id, [])
        if any((e.payload or {}).get("type") == "sent" for e in evs):
            continue
        drafts = [e for e in evs if (e.payload or {}).get("type") == "draft"]
        if drafts:
            drafts.sort(key=lambda e: e.occurred_at, reverse=True)
            pending.append((ob.id, drafts[0].payload))

    if not pending:
        console.print("[dim]no pending drafts.[/dim]")
        return

    for ob_id, payload in pending[:20]:
        console.print(
            f"  {ob_id[:8]}  →  {payload.get('to') or '—':30}  "
            f"{(payload.get('subject') or '—')[:50]}"
        )
    console.print("[dim]Send: /send <id>[/dim]")


def _send_draft_inline(*, db, settings, target: str) -> None:
    """Send a drafted reply via SMTP from inside chat."""
    from sqlmodel import select

    from agent_core.secrets import default_store
    from agent_core.state.models import Obligation
    from agent_core.work.email_send import (
        EmailSender,
        EmailSendError,
        send_draft,
    )

    if db is None:
        console.print("[yellow]/send requires a database; aborting.[/yellow]")
        return

    with db.session() as s:
        obs = list(s.exec(select(Obligation)).all())
        match = next(
            (ob for ob in obs if ob.id == target or ob.id.startswith(target)),
            None,
        )
    if match is None:
        console.print(f"[yellow]no obligation matching {target!r}[/yellow]")
        return

    try:
        sender = EmailSender.from_settings(settings.settings, default_store())
    except EmailSendError as e:
        console.print(f"[red]SMTP not configured:[/red] {e}")
        return

    report = send_draft(db=db, sender=sender, obligation_id=match.id)
    if report.sent:
        console.print(f"[green]✓[/green] sent → {report.to}")
    else:
        console.print(
            f"[red]send failed:[/red] {report.reason}"
            + (f" — {report.error}" if report.error else "")
        )


def _capture_inline(*, db, raw: str) -> str:
    """Add an inbox obligation from a raw chat-typed payload.

    Heuristic: if the first line looks like ``Email from <addr>: <subject>``,
    treat it as an inbound-email obligation (so /triage will pick it up).
    Otherwise, treat as a generic manual obligation.
    """
    from agent_core.state.models import (
        Obligation,
        ObligationSource,
        ObligationStatus,
    )

    lines = raw.split("\n", 1)
    head = lines[0].strip()
    body = lines[1].strip() if len(lines) > 1 else ""

    if head.lower().startswith("email from "):
        title = head
        source = ObligationSource.inbound_email
    else:
        title = head[:200]
        source = ObligationSource.manual

    with db.session() as s:
        ob = Obligation(
            title=title,
            body=body or None,
            source=source,
            status=ObligationStatus.inbox,
        )
        s.add(ob)
        s.commit()
        return ob.id


def _smart_stub_lm():
    """Build a StubLanguageModel with canned plausible responses per skill.

    Pattern-matched by the skill's system prompt — each shipped skill's
    expected output shape is hardcoded here so `<product> skills run` and
    `<product> chat --stub-llm` succeed end-to-end without a real LLM.
    """
    from agent_core.skills import StubLanguageModel

    return StubLanguageModel(
        patterns=[
            (
                r"email triage classifier",
                '{"action": "flag", "score": 0.85, '
                '"reasoning": "stub: would be classified by real LLM"}',
            ),
            (
                r"email drafter",
                "SUBJECT: (stub draft subject)\n---\nThis is a stub draft body.\nBest,\nStub",
            ),
            (
                r"document writer",
                "(Stub draft body — would be written by a real LLM. "
                "Replace this stub with a real provider via "
                "`<product> init --llm-provider …`.)",
            ),
        ],
        default="(stub-llm response — no skill-specific pattern matched)",
    )


__all__ = [
    "chat_command",
    "recall_command",
    "remember_command",
]
