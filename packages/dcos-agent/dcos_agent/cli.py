"""dcos-agent CLI — ``dcos <command>``.

Wraps agent-core's command groups (``settings``, ``ops``) and adds dcos-
specific defaults (XDG paths, "single-user CoS" framing in --help).

The whole point of this thin product package is to give end users a CLI
that *feels* like its own product, not "agent-core for case 1 of 2." Under
the hood it's a Click multi-command tree assembled from agent-core groups.

Top-level commands:

    dcos settings show / set / reset / preset / path / doctor
    dcos doctor
    dcos backup / restore
    dcos setup --tier 1|2|3
    dcos info               — print resolved paths + versions
    dcos skills list        — list registered skills + tags
    dcos skills describe X  — schema + seed rules for skill X
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agent_core.migrations.cli import migrate_group
from agent_core.ops.cli import (
    backup_command,
    doctor_command,
    init_command,
    restore_command,
    setup_command,
)
from agent_core.settings.cli import settings_group
from agent_core.web.cli import serve_command

from dcos_agent import __version__
from dcos_agent.defaults import (
    INSTANCE_NAME,
    config_dir,
    default_db_path,
    default_db_url,
    default_settings_path,
    state_dir,
)

console = Console()


# ── Top-level group ────────────────────────────────────────────────────────


@click.group(
    name="dcos",
    help=(
        "dcos-agent — your personal AI chief of staff. Single-user, SQLite-backed,\n"
        "built on agent-core. Run `dcos info` to see where things live, or\n"
        "`dcos setup` for the guided install wizard."
    ),
)
@click.version_option(__version__)
def cli() -> None:
    """Top-level dcos command."""


# ── Subcommands borrowed from agent-core ───────────────────────────────────

# settings: full agent-core surface, mounted under `dcos settings`
cli.add_command(settings_group, name="settings")

# migrate: one-shot data conversions into backup-format JSON
cli.add_command(migrate_group, name="migrate")

# Each ops command needs --config defaulted to dcos's config path. Click
# doesn't easily let us override defaults on borrowed commands, so we wrap
# each one with a thin shim that sets the right defaults.


@cli.command(name="doctor")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="dcos config dir",
    help="Path to agent.yml.",
)
@click.option(
    "--db-url",
    default=lambda: default_db_url() if default_db_path().exists() else None,
    show_default="dcos sqlite path if it exists",
    help="SQLAlchemy URL for the agent database.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.pass_context
def doctor(ctx, config_path, db_url, as_json):
    """Run health checks against the dcos install."""
    ctx.invoke(doctor_command, config_path=config_path, db_url=db_url, as_json=as_json)


@cli.command(name="backup")
@click.argument("output", type=click.Path(path_type=Path))
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="dcos config",
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
    show_default="dcos sqlite path",
)
@click.pass_context
def backup(ctx, output, config_path, db_url):
    """Snapshot dcos state to a portable JSON file."""
    ctx.invoke(
        backup_command,
        output=output,
        config_path=config_path,
        db_url=db_url,
        include_identity_public_key=None,
    )


@cli.command(name="restore")
@click.argument("source", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
    show_default="dcos sqlite path",
)
@click.option(
    "--settings-path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="dcos config",
    help="Where to write the embedded agent.yml (if any).",
)
@click.option("--yes", is_flag=True)
@click.option("--skip-schema-check", is_flag=True)
@click.pass_context
def restore(ctx, source, db_url, settings_path, yes, skip_schema_check):
    """Restore dcos state from a backup file (DESTRUCTIVE)."""
    ctx.invoke(
        restore_command,
        source=source,
        db_url=db_url,
        settings_path=settings_path,
        yes=yes,
        skip_schema_check=skip_schema_check,
    )


@cli.command(name="setup")
@click.option(
    "--tier",
    type=click.IntRange(1, 3),
    default=1,
    help="1 = three questions; 2 = + integrations + push; 3 = every knob.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="dcos config dir",
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
    show_default="dcos sqlite path",
    help="SQLAlchemy URL forwarded to init (default: dcos XDG path).",
)
@click.option("--no-init", is_flag=True)
@click.option("--no-doctor", is_flag=True)
@click.pass_context
def setup(ctx, tier, config_path, db_url, no_init, no_doctor):
    """Interactive setup wizard. Runs init + doctor at the end by default."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # Make sure the db dir exists so init's alembic upgrade can create the file.
    default_db_path().parent.mkdir(parents=True, exist_ok=True)
    ctx.invoke(
        setup_command,
        tier=tier,
        config_path=config_path,
        db_url=db_url,
        no_init=no_init,
        no_doctor=no_doctor,
    )


@cli.command(name="init")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="dcos config dir",
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
    show_default="dcos sqlite path",
)
@click.option("--rotate-token", is_flag=True, help="Generate a new API token even if one exists.")
@click.option(
    "--llm-provider",
    type=click.Choice(["stub", "openai_compat", "ollama"]),
    default=None,
)
@click.option("--llm-base-url", default=None)
@click.option("--llm-model", default=None)
@click.option("--llm-api-key", default=None)
@click.pass_context
def init(
    ctx,
    config_path,
    db_url,
    rotate_token,
    llm_provider,
    llm_base_url,
    llm_model,
    llm_api_key,
):
    """Bootstrap the schema + generate an API token. Run after `setup`."""
    default_db_path().parent.mkdir(parents=True, exist_ok=True)
    ctx.invoke(
        init_command,
        config_path=config_path,
        db_url=db_url,
        rotate_token=rotate_token,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
    )


@cli.command(name="serve")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="dcos config dir",
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
    show_default="dcos sqlite path",
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--token", "api_token", default=None, help="Override API token (default: from secrets store).")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (development only).")
@click.pass_context
def serve(ctx, config_path, db_url, host, port, api_token, reload):
    """Start the agent_core.web FastAPI server (the OpenWebUI plugin's backend)."""
    ctx.invoke(
        serve_command,
        config_path=config_path,
        db_url=db_url,
        host=host,
        port=port,
        api_token=api_token,
        reload=reload,
    )


# ── dcos-specific commands ────────────────────────────────────────────────


@cli.command(name="run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
)
@click.option(
    "--interval",
    type=int,
    default=300,
    show_default=True,
    help="Seconds between ticks. Default 5 minutes.",
)
@click.option(
    "--once",
    is_flag=True,
    help="Run a single tick + exit. Useful for cron / CI / debugging.",
)
def run(config_path, db_url, interval, once):
    """Periodic agent loop. Scans for stalled obligations + notifies.

    Foreground process — Ctrl-C to stop. Designed to run as a
    long-lived process (launchd unit on macOS, systemd --user unit on
    Linux). Each tick:

    \b
      1. Scans the obligation board for stalled items (per
         settings.work.pipeline_*_threshold_hours).
      2. Opens an Incident row for any newly-stalled obligation
         (idempotent — won't re-open already-flagged ones).
      3. Sends a notification per the configured NotificationSettings.

    Pair with `dcos serve` (HTTP API) — they're independent, run them
    in separate terminals.
    """
    from agent_core.agent.run_loop import run_loop
    from agent_core.notifications import NotificationDispatcher
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    if not db_url:
        db_url = mgr.get("storage.url")
    db = Database(db_url)

    try:
        dispatcher = NotificationDispatcher.from_settings(mgr.settings)
    except Exception as e:
        console.print(f"[yellow]notifications disabled:[/yellow] {e}")
        dispatcher = None

    if once:
        console.print("[dim]running one tick (--once)…[/dim]")
    else:
        console.print(
            f"[dim]agent loop running (every {interval}s). Ctrl-C to stop.[/dim]"
        )

    tick_count = run_loop(
        db=db,
        settings=mgr,
        dispatcher=dispatcher,
        interval_seconds=interval,
        once=once,
    )

    if not once:
        console.print(f"[dim]ran {tick_count} ticks. bye.[/dim]")


@cli.command(name="digest")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
)
@click.option(
    "--hours",
    type=float,
    default=None,
    help="Window size in hours. Defaults to settings.notifications.digest_period_hours (24).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the raw DailyDigest dataclass as JSON instead of markdown.",
)
@click.option(
    "--send",
    is_flag=True,
    help=(
        "Push the digest through the notification dispatcher (ntfy/etc) "
        "in addition to printing it. Bypasses the urgency floor — explicit user "
        "intent always reaches the transport."
    ),
)
@click.option(
    "--respect-cadence",
    is_flag=True,
    help=(
        "With --send, skip if a digest was already delivered within the "
        "period window. Useful when wired into cron/launchd. Default is "
        "force-send for explicit CLI calls."
    ),
)
@click.option(
    "--respect-floor",
    is_flag=True,
    help=(
        "With --send, honor settings.notifications.urgency_floor. Default "
        "is to bypass it (explicit CLI call = the user wants it). Useful "
        "when this is wired into cron and you want one knob in settings."
    ),
)
@click.option(
    "--send-when-empty",
    is_flag=True,
    help="With --send, deliver even when the digest has no content.",
)
def digest(config_path, db_url, hours, as_json, send, respect_cadence, respect_floor, send_when_empty):
    """Render a daily digest of what the agent has been up to.

    Aggregates the past 24h (or --hours) of:

    \b
      - Closed obligations
      - Auto-triage decisions (Sprint 17: dcos run)
      - Newly opened incidents (Sprint 16: stalled detection)
      - Failed actions
      - External-facing actions (email sends, publishes)
      - Open carry-over incidents

    Run after a long `dcos run` session to see what happened, or wire
    into cron/launchd to email yourself a morning summary.
    """
    import json as _json
    from dataclasses import asdict

    from agent_core.actions.digest import DailyDigestBuilder
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    if not db_url:
        db_url = mgr.get("storage.url")
    db = Database(db_url)

    if hours is not None:
        builder = DailyDigestBuilder(db, period_hours=hours)
    else:
        builder = DailyDigestBuilder.from_settings(mgr.settings, db)

    if send:
        from agent_core.actions.digest import deliver_digest
        from agent_core.notifications import NotificationDispatcher

        try:
            dispatcher = NotificationDispatcher.from_settings(mgr.settings)
        except Exception as e:
            console.print(f"[red]notifications not configured:[/red] {e}")
            console.print(
                "[dim]Hint: `dcos settings set notifications.transport=ntfy "
                "notifications.ntfy_topic=<your-private-topic> "
                "notifications.enabled=true`.[/dim]"
            )
            raise click.exceptions.Exit(1) from e

        report = deliver_digest(
            db=db,
            dispatcher=dispatcher,
            builder=builder,
            force=not respect_cadence,
            bypass_floor=not respect_floor,
            send_when_empty=send_when_empty,
        )
        emoji = "[green]✓[/green]" if report.sent else "[yellow]∅[/yellow]"
        console.print(
            f"{emoji} digest delivery: {report.reason} "
            f"(transport={report.transport})"
        )
        if report.last_sent_at:
            console.print(f"[dim]last sent: {report.last_sent_at.isoformat()}[/dim]")
        if not report.sent and report.next_eligible_at:
            console.print(
                f"[dim]next eligible: {report.next_eligible_at.isoformat()}"
                " (use --force or wait)[/dim]"
            )
        # Also print the rendered digest unless we were JSON-mode (caller
        # presumably wants machine output and the dispatcher already pushed
        # it to the human side).
        d = report.digest or builder.build()
        if as_json:
            click.echo(_json.dumps(asdict(d), default=str, indent=2, sort_keys=True))
        else:
            click.echo()
            click.echo(d.as_markdown())
        return

    d = builder.build()

    if as_json:
        # Datetimes need stringifying for JSON.
        click.echo(_json.dumps(asdict(d), default=str, indent=2, sort_keys=True))
        return

    click.echo(d.as_markdown())


@cli.command(name="remember")
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
    default=lambda: default_settings_path(),
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
)
@click.option(
    "--from-stdin",
    is_flag=True,
    help="Read content from stdin instead of CLI args. Useful for piping.",
)
def remember(
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
        dcos remember "Robyne prefers Tuesday meetings"
        echo "long content..." | dcos remember --from-stdin
        dcos remember "Charlotte SMS" --source-kind sms --source-uri 555-1234
    """
    import sys as _sys

    from agent_core.openbrain import OpenBrainStore
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database

    if from_stdin:
        text = _sys.stdin.read().strip()
    else:
        text = " ".join(content).strip()

    if not text:
        console.print(
            "[red]nothing to remember:[/red] pass content as args or --from-stdin"
        )
        raise click.exceptions.Exit(2)

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    if not db_url:
        db_url = mgr.get("storage.url")
    db = Database(db_url)
    store = OpenBrainStore.from_settings(mgr.settings, db)

    thought = store.capture(
        text,
        source_kind=source_kind,
        source_uri=source_uri,
        source_title=source_title,
    )
    console.print(f"[green]captured[/green] id={thought.id[:8]}…")
    console.print(
        f"[dim]source_kind={source_kind}{' uri='+source_uri if source_uri else ''}[/dim]"
    )
    if len(text) > 100:
        console.print(f"[dim]content: {text[:100]}…[/dim]")
    else:
        console.print(f"[dim]content: {text}[/dim]")


@cli.command(name="recall")
@click.argument("query", nargs=-1, required=True)
@click.option("--limit", default=5, type=int, show_default=True)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
)
def recall(query, limit, config_path, db_url):
    """Semantic search across captured thoughts.

    Pairs with `dcos remember`. Hits include similarity scores + source
    provenance so you can trace each result.

    \b
        dcos remember "Robyne prefers Tuesday meetings"
        dcos recall meetings with Robyne
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

    if not db_url:
        db_url = mgr.get("storage.url")
    db = Database(db_url)
    store = OpenBrainStore.from_settings(mgr.settings, db)

    hits = store.search(text, limit=limit)
    if not hits:
        console.print("[dim]no hits[/dim]")
        return

    for i, h in enumerate(hits, start=1):
        sim = round(h.similarity, 3)
        src = h.sources[0] if h.sources else None
        src_str = f" ({src.source_kind})" if src else ""
        console.print(
            f"[bold cyan]{i}.[/bold cyan] [dim]similarity={sim}{src_str}[/dim]"
        )
        snippet = h.thought.content[:300].replace("\n", " ")
        console.print(f"   {snippet}")
        console.print()


@cli.command(name="chat")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="dcos config dir",
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
    show_default="dcos sqlite path",
)
@click.option(
    "--no-context",
    is_flag=True,
    help="Don't inject obligations + openbrain hits into the system prompt.",
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
def chat(config_path, db_url, no_context, system_prompt, max_tokens, stub_llm):
    """Talk to your agent in a CLI REPL.

    Loads the configured LLM (per ``settings.llm.provider``), opens a
    conversation, and injects active obligations + relevant openbrain
    hits into each turn's system prompt by default. Type ``/exit`` or
    Ctrl-D to leave.

    Quick LLM setup if you haven't yet::

        dcos init --llm-provider openai_compat --llm-api-key "$OPENAI_API_KEY"

    Or for free local chat::

        dcos init --llm-provider ollama --llm-model llama3.2
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

    db = Database(db_url) if db_url else None
    openbrain = OpenBrainStore.from_settings(mgr.settings, db) if db else None

    if stub_llm:
        lm = _smart_stub_lm()
        provider_label = "stub-llm (forced)"
    else:
        try:
            lm = language_model_from_settings(mgr.settings, default_store())
            provider_label = (
                f"{mgr.settings.llm.provider} / {mgr.settings.llm.model}"
            )
        except LanguageModelError as e:
            console.print(f"[red]LLM not configured:[/red] {e}")
            console.print(
                "[dim]Run [cyan]dcos init --llm-provider openai_compat "
                "--llm-api-key sk-...[/cyan] (or --llm-provider ollama for "
                "local), or use [cyan]--stub-llm[/cyan].[/dim]"
            )
            raise click.exceptions.Exit(1) from e

    import uuid as _uuid

    session = ChatSession(
        system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
        inject_obligations=not no_context and db is not None,
        inject_openbrain=not no_context and openbrain is not None,
        session_id=f"cli-{_uuid.uuid4()}",
    )

    console.print(f"[dim]chatting with {provider_label}. Ctrl-D or /exit to quit.[/dim]")
    console.print(
        "[dim]Slash commands: /help, /reset, /context, /triage, /run, /digest, "
        "/capture, /exit.[/dim]"
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
                "/triage        run inbox auto-triage now (dcos run --once, triage only)\n"
                "/run           run a single autonomous tick (stalled detection + triage)\n"
                "/digest [hrs]  show recent agent activity (default 24h)\n"
                "/capture <text> add an inbox obligation; next /triage will classify it\n"
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
                console.print(f"[yellow]/digest expects a number of hours, got {parts[1]!r}[/yellow]")
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
        if text.startswith("/"):
            console.print(f"[yellow]unknown command:[/yellow] {text}")
            continue

        try:
            reply = run_turn(
                user_message=text,
                session=session,
                language_model=lm,
                db=db,
                openbrain=openbrain,
                max_tokens=max_tokens,
            )
        except LanguageModelError as e:
            console.print(f"[red]LLM error:[/red] {e}")
            continue
        except KeyboardInterrupt:
            console.print("\n[dim](interrupted)[/dim]")
            continue

        console.print(f"[bold cyan]agent:[/bold cyan] {reply}")
        console.print()


@cli.command(name="info")
def info() -> None:
    """Show resolved dcos paths + version. Useful in bug reports."""
    table = Table(title="dcos-agent", show_header=False)
    table.add_column("key", style="cyan", no_wrap=True)
    table.add_column("value")
    table.add_row("version", __version__)
    table.add_row("instance", INSTANCE_NAME)
    table.add_row("config dir", str(config_dir()))
    table.add_row("settings file", str(default_settings_path()))
    table.add_row("state dir", str(state_dir()))
    table.add_row("db path", str(default_db_path()))
    table.add_row("db url", default_db_url())
    table.add_row(
        "config exists",
        "yes" if default_settings_path().exists() else "no (run `dcos setup`)",
    )
    table.add_row(
        "db exists",
        "yes" if default_db_path().exists() else "no (will be created on first run)",
    )
    console.print(table)


# ── Skills subgroup ───────────────────────────────────────────────────────

# Side-effecting import: registers the three default skills (email-triage,
# document-creator, email-composer) into agent_core.skills.default_registry.
import dcos_agent.skills  # noqa: F401, E402


@cli.group(name="skills")
def skills_group() -> None:
    """Discover + describe registered skills."""


@skills_group.command(name="list")
def skills_list() -> None:
    """Show every registered skill with its tags."""
    from agent_core.skills import default_registry

    skills = default_registry.list()
    if not skills:
        console.print("[dim]no skills registered[/dim]")
        return
    table = Table(title=f"{len(skills)} registered skill(s)")
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("tags", style="dim")
    table.add_column("description")
    for skill in skills:
        table.add_row(skill.name, ", ".join(skill.tags), skill.description)
    console.print(table)


@skills_group.command(name="describe")
@click.argument("name")
def skills_describe(name: str) -> None:
    """Print the input/output schemas + seed rules for skill NAME."""
    import json

    from agent_core.skills import default_registry

    skill = default_registry.get(name)
    if skill is None:
        console.print(f"[red]no skill named[/red] {name}")
        console.print(f"[dim]registered: {', '.join(default_registry.names())}[/dim]")
        raise click.exceptions.Exit(1)

    console.print(f"[bold cyan]{skill.name}[/bold cyan] — {skill.description}")
    console.print(f"[dim]tags:[/dim] {', '.join(skill.tags)}\n")

    console.print("[bold]Input schema:[/bold]")
    console.print(json.dumps(skill.input_schema.model_json_schema(), indent=2))
    console.print()

    console.print("[bold]Output schema:[/bold]")
    console.print(json.dumps(skill.output_schema.model_json_schema(), indent=2))
    console.print()

    if skill.seed_rules:
        console.print(f"[bold]Seed rules ({len(skill.seed_rules)}):[/bold]")
        for r in skill.seed_rules:
            console.print(f"  • {r.correction}")


@skills_group.command(name="run")
@click.argument("name")
@click.option(
    "--input",
    "input_str",
    default=None,
    help=(
        "Input as JSON string. Use @path/to/file.json to read from a file, "
        "or @- to read from stdin."
    ),
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="dcos config dir",
)
@click.option(
    "--db-url",
    default=None,
    help="SQLAlchemy URL. Defaults to settings.storage.url.",
)
@click.option(
    "--stub-llm",
    is_flag=True,
    help=(
        "Force the StubLanguageModel even if a real LLM is configured. Useful "
        "for verifying skill wiring without LLM cost."
    ),
)
def skills_run(
    name: str,
    input_str: str | None,
    config_path: Path,
    db_url: str | None,
    stub_llm: bool,
) -> None:
    """Invoke a registered skill with INPUT JSON. Prints the result.

    Default LLM is whatever ``settings.llm.provider`` says — stub for
    fresh installs, openai_compat / ollama once configured. Pass
    ``--stub-llm`` to force the smart-stub even when a real LLM is
    configured (useful for offline wiring smoke).
    """
    import json
    import sys as _sys

    from agent_core.openbrain import OpenBrainStore
    from agent_core.secrets import default_store
    from agent_core.settings import SettingsManager
    from agent_core.skills import (
        LanguageModelError,
        SkillContext,
        SkillRunner,
        default_registry,
        language_model_from_settings,
    )
    from agent_core.state.db import Database

    # Resolve input
    if input_str is None:
        payload: dict = {}
    elif input_str == "@-":
        payload = json.loads(_sys.stdin.read())
    elif input_str.startswith("@"):
        with open(input_str[1:]) as f:
            payload = json.load(f)
    else:
        payload = json.loads(input_str)

    # Resolve settings + db
    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    resolved_url = db_url or mgr.get("storage.url")
    db = Database(resolved_url) if resolved_url else None

    # Build the LanguageModel. ``--stub-llm`` forces the smart stub
    # regardless of settings; otherwise we honor whatever's configured.
    if stub_llm:
        lm = _smart_stub_lm()
    else:
        try:
            lm = language_model_from_settings(mgr.settings, default_store())
        except LanguageModelError as e:
            console.print(f"[red]LLM not configured:[/red] {e}")
            console.print(
                "[dim]Hint: pass --stub-llm to run with canned responses, "
                "or `dcos init --llm-provider openai_compat --llm-api-key sk-...`.[/dim]"
            )
            raise click.exceptions.Exit(1) from e
        # If settings says provider=stub (fresh install default), make it
        # smart so the shipped skills still work for the wiring smoke.
        if getattr(mgr.settings.llm, "provider", "stub") == "stub":
            lm = _smart_stub_lm()

    openbrain = (
        OpenBrainStore.from_settings(mgr.settings, db) if db else None
    )
    ctx = SkillContext(
        settings=mgr.settings,
        db=db,
        language_model=lm,
        openbrain=openbrain,
    )

    runner = SkillRunner(default_registry)
    outcome = runner.run(name, payload, ctx)

    if not outcome.succeeded:
        console.print(f"[red]skill failed:[/red] {outcome.error}")
        raise click.exceptions.Exit(1)

    result = outcome.result
    console.print(f"[green]✓[/green] {name} succeeded "
                  f"(confidence={result.confidence:.2f})")
    if result.rationale:
        console.print(f"[dim]rationale:[/dim] {result.rationale}")
    console.print()
    console.print("[bold]output:[/bold]")
    console.print(json.dumps(result.output.model_dump(), indent=2, default=str))
    if result.references:
        console.print()
        console.print(f"[bold]references ({len(result.references)}):[/bold]")
        for ref in result.references[:5]:
            console.print(f"  • {ref}")


# ── Helpers ───────────────────────────────────────────────────────────────


def _run_triage_inline(*, db, settings, language_model) -> None:
    """Run triage_inbox and print a one-line summary to the chat console."""
    from agent_core.agent.run_loop import triage_inbox
    import dcos_agent.skills  # noqa: F401  ensures email-triage is registered

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
    from agent_core.agent.run_loop import run_tick
    from agent_core.notifications import NotificationDispatcher
    import dcos_agent.skills  # noqa: F401

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
    console.print(
        f"[dim]tick: {report.stalled_count} stalled, "
        f"{report.new_incidents} new incidents, "
        f"{report.notifications_sent} notifications sent. "
        f"triage: {triage.candidates} candidates, {triage.triaged} classified."
        f"[/dim]"
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
    expected output shape is hardcoded here so `dcos skills run <name>`
    succeeds end-to-end without a real LLM. When Hermes vendoring lands
    we'll switch to a real LanguageModel and this helper retires.
    """
    from agent_core.skills import StubLanguageModel

    return StubLanguageModel(
        patterns=[
            # email-triage expects a strict JSON {action, score, reasoning}
            (
                r"email triage classifier",
                '{"action": "flag", "score": 0.85, "reasoning": "stub: would be classified by real LLM"}',
            ),
            # email-composer expects "SUBJECT: <line>\n---\n<body>"
            (
                r"email drafter",
                "SUBJECT: (stub draft subject)\n---\nThis is a stub draft body.\nBest,\nStub",
            ),
            # document-creator expects free-form prose
            (
                r"document writer",
                "(Stub draft body — would be written by a real LLM. Replace this stub when Hermes lands.)",
            ),
        ],
        default="(stub-llm response — no skill-specific pattern matched)",
    )


# ── Entry point ───────────────────────────────────────────────────────────


def main() -> None:
    """dcos-agent entry point.

    Sets ``AGENT_DATA_DIR`` so the agent-core ``settings`` group (which we
    borrow wholesale via ``cli.add_command``) resolves its default
    ``--config`` path to ``~/.config/dcos-agent/agent.yml`` instead of
    ``cwd/agent.yml``. Without this, ``dcos settings set foo=bar`` writes
    to whatever directory the user happened to run from — confusing and
    inconsistent with every other dcos subcommand (which already pass
    ``--config default_settings_path()`` explicitly).

    Honors any pre-existing AGENT_DATA_DIR — power users overriding the
    location stay in control.
    """
    import os

    os.environ.setdefault("AGENT_DATA_DIR", str(config_dir()))
    cli()


if __name__ == "__main__":
    main()
