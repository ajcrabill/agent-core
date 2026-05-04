"""Shared `email` CLI group.

Mounted via ``cli.add_command(email_group, name="email")`` in both
dcos-agent and ikb-agent. Subcommands:

  email pull              — IMAP fetch, dedupe, capture as obligations
  email drafts            — list pending drafts awaiting approval
  email show <id>         — preview a specific draft
  email compose           — manually invoke email-composer on triaged-as-draft
  email send <id>         — SMTP-deliver a draft, mark obligation done

All commands default ``--config`` and ``--db-url`` to None and resolve
through SettingsManager + settings.storage.url. Product main()s set
AGENT_DATA_DIR so the right config dir is used.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.group(name="email")
def email_group() -> None:
    """Email integration — IMAP inbound + SMTP outbound."""


# ── pull ───────────────────────────────────────────────────────────────────


@email_group.command(name="pull")
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
    "--limit",
    type=int,
    default=None,
    help="Max messages to fetch this run. Defaults to settings.email.imap.fetch_limit.",
)
def email_pull(config_path, db_url, limit):
    """Pull unread email from IMAP into the obligation board.

    Each message becomes an inbox-status, inbound_email-source obligation,
    deduplicated by Message-ID so re-runs don't double-capture. The next
    `run` tick (or chat /triage) will classify them via the email-triage
    skill.

    \b
    First-time setup:
      <product> settings set email.imap.host=imap.gmail.com
      <product> settings set email.imap.username=you@example.com
      <product> secrets set email.imap_password
      <product> settings set email.imap.enabled=true
      <product> email pull
    """
    from agent_core.secrets import default_store
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database
    from agent_core.work.email_fetch import (
        EmailFetchError,
        EmailFetcher,
        fetch_and_capture,
    )

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
        fetcher = EmailFetcher.from_settings(mgr.settings, default_store())
    except EmailFetchError as e:
        console.print(f"[red]email fetch not configured:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    effective_limit = (
        limit if limit is not None else mgr.settings.email.imap.fetch_limit
    )
    console.print(
        f"[dim]connecting to {fetcher.host}:{fetcher.port} as {fetcher.username}…[/dim]"
    )
    report = fetch_and_capture(fetcher=fetcher, db=db, limit=effective_limit)

    console.print(
        f"[green]fetched[/green] {report.fetched}, "
        f"[cyan]captured[/cyan] {report.captured}, "
        f"[dim]skipped {report.skipped_duplicate} duplicate[/dim]"
    )
    for err in report.errors:
        console.print(f"[red]error:[/red] {err}")
    if report.errors:
        raise click.exceptions.Exit(1)


# ── drafts (list) ──────────────────────────────────────────────────────────


@email_group.command(name="drafts")
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
    "--limit",
    type=int,
    default=20,
    show_default=True,
)
def email_drafts(config_path, db_url, limit):
    """List pending email drafts (composed but not yet sent).

    A draft is an ObligationEvent of kind=comment with payload.type='draft'
    on an in-progress, inbound_email obligation. Send one with
    `<product> email send <obligation-id>`.
    """
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database
    from agent_core.state.models import (
        Obligation,
        ObligationEvent,
        ObligationEventKind,
        ObligationSource,
        ObligationStatus,
    )
    from sqlmodel import select

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

    with db.session() as s:
        obs = list(
            s.exec(
                select(Obligation)
                .where(Obligation.source == ObligationSource.inbound_email)
                .where(Obligation.status == ObligationStatus.in_progress)
                .order_by(Obligation.created_at.desc())
                .limit(limit * 3)
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

    by_obligation: dict[str, list] = {}
    for ev in events:
        by_obligation.setdefault(ev.obligation_id, []).append(ev)

    pending: list[tuple[Obligation, dict]] = []
    for ob in obs:
        evs = by_obligation.get(ob.id, [])
        sent = any((e.payload or {}).get("type") == "sent" for e in evs)
        if sent:
            continue
        drafts = [e for e in evs if (e.payload or {}).get("type") == "draft"]
        if drafts:
            drafts.sort(key=lambda e: e.occurred_at, reverse=True)
            pending.append((ob, drafts[0].payload))

    if not pending:
        console.print("[dim]no pending drafts.[/dim]")
        return

    pending = pending[:limit]
    table = Table(title=f"pending email drafts ({len(pending)})")
    table.add_column("obligation", style="cyan", no_wrap=True)
    table.add_column("to")
    table.add_column("subject")
    for ob, payload in pending:
        table.add_row(
            ob.id[:8],
            (payload.get("to") or "—")[:40],
            (payload.get("subject") or "—")[:60],
        )
    console.print(table)
    console.print("[dim]Preview: email show <id>   Send: email send <id>[/dim]")


# ── show ───────────────────────────────────────────────────────────────────


@email_group.command(name="show")
@click.argument("obligation_id")
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
def email_show(obligation_id, config_path, db_url):
    """Print the latest draft for an obligation (full body)."""
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database
    from agent_core.state.models import (
        Obligation,
        ObligationEvent,
        ObligationEventKind,
    )
    from sqlmodel import select

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

    with db.session() as s:
        obs = list(s.exec(select(Obligation)).all())
        match = next(
            (
                ob
                for ob in obs
                if ob.id == obligation_id or ob.id.startswith(obligation_id)
            ),
            None,
        )
        if match is None:
            console.print(f"[red]no obligation matching {obligation_id!r}[/red]")
            raise click.exceptions.Exit(2)
        events = list(
            s.exec(
                select(ObligationEvent)
                .where(ObligationEvent.obligation_id == match.id)
                .where(ObligationEvent.kind == ObligationEventKind.comment)
                .order_by(ObligationEvent.occurred_at.desc())
            ).all()
        )

    drafts = [e for e in events if (e.payload or {}).get("type") == "draft"]
    if not drafts:
        console.print(f"[yellow]no draft found for {match.id[:8]}[/yellow]")
        raise click.exceptions.Exit(2)
    payload = drafts[0].payload
    console.print(f"[bold]obligation:[/bold] {match.id}")
    console.print(f"[bold]title:[/bold] {match.title}")
    console.print(f"[bold]to:[/bold] {payload.get('to') or '—'}")
    console.print(f"[bold]subject:[/bold] {payload.get('subject') or '—'}")
    if payload.get("in_reply_to"):
        console.print(f"[dim]in-reply-to:[/dim] {payload['in_reply_to']}")
    console.print()
    console.print(payload.get("body") or "[dim](empty body)[/dim]")


# ── compose ────────────────────────────────────────────────────────────────


@email_group.command(name="compose")
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
@click.option("--limit", type=int, default=10, show_default=True)
def email_compose(config_path, db_url, limit):
    """Run email-composer on triaged-as-draft email obligations.

    Same code path the autonomous tick uses when email.auto_compose=true,
    exposed manually so you can compose on demand without enabling
    auto-compose.
    """
    # Importing agent_core.skills triggers registration of the built-in
    # email-composer / email-triage / document-creator skills.
    import agent_core.skills  # noqa: F401
    from agent_core.secrets import default_store
    from agent_core.settings import SettingsManager
    from agent_core.skills import (
        LanguageModelError,
        language_model_from_settings,
    )
    from agent_core.state.db import Database
    from agent_core.work.email_send import compose_drafts

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
        lm = language_model_from_settings(mgr.settings, default_store())
    except LanguageModelError as e:
        console.print(f"[red]LLM not configured:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    report = compose_drafts(db=db, settings=mgr, language_model=lm, limit=limit)
    console.print(
        f"[green]drafted[/green] {report.drafted}, "
        f"[dim]skipped {report.skipped_already_drafted} already drafted[/dim]"
    )
    for err in report.errors:
        console.print(f"[red]error:[/red] {err}")
    if report.errors:
        raise click.exceptions.Exit(1)


# ── send ───────────────────────────────────────────────────────────────────


@email_group.command(name="send")
@click.argument("obligation_id")
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
    "--yes",
    is_flag=True,
    help="Skip the preview + confirmation prompt. Useful for scripts.",
)
def email_send(obligation_id, config_path, db_url, yes):
    """Send a previously-composed draft via SMTP, then mark obligation done.

    By default shows a preview and asks for confirmation. Pass --yes to
    skip the prompt (e.g., from a script that already verified the draft).
    """
    from agent_core.secrets import default_store
    from agent_core.settings import SettingsManager
    from agent_core.state.db import Database
    from agent_core.state.models import (
        Obligation,
        ObligationEvent,
        ObligationEventKind,
    )
    from agent_core.work.email_send import (
        EmailSender,
        EmailSendError,
        send_draft,
    )
    from sqlmodel import select

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

    # Resolve short prefix → full id
    with db.session() as s:
        obs = list(s.exec(select(Obligation)).all())
        match = next(
            (
                ob
                for ob in obs
                if ob.id == obligation_id or ob.id.startswith(obligation_id)
            ),
            None,
        )
        if match is None:
            console.print(f"[red]no obligation matching {obligation_id!r}[/red]")
            raise click.exceptions.Exit(2)
        full_id = match.id

        if not yes:
            events = list(
                s.exec(
                    select(ObligationEvent)
                    .where(ObligationEvent.obligation_id == full_id)
                    .where(ObligationEvent.kind == ObligationEventKind.comment)
                    .order_by(ObligationEvent.occurred_at.desc())
                ).all()
            )
            drafts = [
                e for e in events if (e.payload or {}).get("type") == "draft"
            ]
            if not drafts:
                console.print(
                    f"[yellow]no draft to send for {full_id[:8]}[/yellow]"
                )
                raise click.exceptions.Exit(2)
            p = drafts[0].payload
            console.print(f"[bold]to:[/bold] {p.get('to')}")
            console.print(f"[bold]subject:[/bold] {p.get('subject')}")
            console.print()
            console.print((p.get("body") or "")[:1000])
            console.print()
            if not click.confirm("send this draft?"):
                console.print("[dim]cancelled[/dim]")
                return

    try:
        sender = EmailSender.from_settings(mgr.settings, default_store())
    except EmailSendError as e:
        console.print(f"[red]SMTP not configured:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    report = send_draft(db=db, sender=sender, obligation_id=full_id)
    if report.sent:
        console.print(f"[green]✓[/green] sent → {report.to}")
    else:
        console.print(f"[red]send failed:[/red] {report.reason}")
        if report.error:
            console.print(f"[dim]{report.error}[/dim]")
        raise click.exceptions.Exit(1)


__all__ = ["email_group"]
