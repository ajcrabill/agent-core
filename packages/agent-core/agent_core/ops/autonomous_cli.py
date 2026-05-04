"""Shared `run` and `digest` CLI commands.

Both dcos-agent and ikb-agent want the autonomous-tick loop and the daily
digest. These were originally in dcos's CLI; extracted here so ikb (and
any future product) gets them by mounting:

    cli.add_command(run_command, name="run")
    cli.add_command(digest_command, name="digest")

Settings + DB resolution: both commands default ``--config`` and
``--db-url`` to None. The product's ``main()`` sets ``AGENT_DATA_DIR`` so
``SettingsManager()`` reads from the right place; ``--db-url`` falls
back to ``settings.storage.url`` (which the wizard wrote for you).
"""

from __future__ import annotations

import json as _json
from dataclasses import asdict
from pathlib import Path

import click
from rich.console import Console

console = Console()


# ── run ────────────────────────────────────────────────────────────────────


@click.command(name="run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to agent.yml. Defaults to $AGENT_DATA_DIR/agent.yml.",
)
@click.option(
    "--db-url",
    default=None,
    help="SQLAlchemy URL. When omitted, reads settings.storage.url.",
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
def run_command(config_path, db_url, interval, once):
    """Periodic agent loop. Stalled-detection + email triage + digest delivery.

    Foreground process — Ctrl-C to stop. Designed to run as a long-lived
    process (launchd unit on macOS, systemd --user unit on Linux). Each
    tick runs:

    \b
      1. Stalled-obligation scan → opens incidents on newly-stalled items
      2. Notification dispatch for newly-stalled (per urgency floor)
      3. IMAP fetch (if email.imap.enabled) → captures + dedupes new mail
      4. Auto-triage on inbox-status email obligations (LLM call)
      5. Compose drafts (if email.auto_compose) for triaged-as-draft items
      6. Cadence-gated digest delivery (if dispatcher configured)
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

    resolved_url = db_url or mgr.get("storage.url")
    if not resolved_url:
        console.print(
            "[red]no db url:[/red] pass --db-url or set storage.url in agent.yml"
        )
        raise click.exceptions.Exit(1)
    db = Database(resolved_url)

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


# ── digest ─────────────────────────────────────────────────────────────────


@click.command(name="digest")
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
        "in addition to printing it. Bypasses the urgency floor — explicit "
        "user intent always reaches the transport."
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
        "is to bypass it (explicit CLI call = the user wants it)."
    ),
)
@click.option(
    "--send-when-empty",
    is_flag=True,
    help="With --send, deliver even when the digest has no content.",
)
def digest_command(
    config_path, db_url, hours, as_json, send, respect_cadence, respect_floor, send_when_empty
):
    """Render a daily digest of what the agent has been up to.

    Aggregates the past 24h (or --hours) of:

    \b
      - Closed obligations
      - Auto-triage decisions (sprint 17)
      - Newly opened incidents (sprint 16: stalled detection)
      - Failed actions
      - External-facing actions (email sends, publishes)
      - Open carry-over incidents
    """
    from agent_core.actions.digest import DailyDigestBuilder
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    resolved_url = db_url or mgr.get("storage.url")
    if not resolved_url:
        console.print(
            "[red]no db url:[/red] pass --db-url or set storage.url in agent.yml"
        )
        raise click.exceptions.Exit(1)
    db = Database(resolved_url)

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
                "[dim]Hint: `<product> settings set notifications.transport=ntfy "
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
        d = report.digest or builder.build()
        if as_json:
            click.echo(_json.dumps(asdict(d), default=str, indent=2, sort_keys=True))
        else:
            click.echo()
            click.echo(d.as_markdown())
        return

    d = builder.build()

    if as_json:
        click.echo(_json.dumps(asdict(d), default=str, indent=2, sort_keys=True))
        return

    click.echo(d.as_markdown())


__all__ = ["digest_command", "run_command"]
