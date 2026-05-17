"""Shared `calendar` CLI group — read-only ICS feed.

Mounted via ``cli.add_command(calendar_group, name="calendar")`` in both
dcos-agent and ikb-agent. Backed by ``agent_core.work.calendar``.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

console = Console()


def _print_calendar_events(events) -> None:
    if not events:
        console.print("[dim]nothing on the calendar.[/dim]")
        return
    for ev in events:
        if ev.all_day:
            line = f"[dim](all day)[/dim] {ev.summary}"
        else:
            time_str = ev.start.strftime("%H:%M")
            line = f"[cyan]{time_str}[/cyan]  {ev.summary}"
        if ev.location:
            line += f" [dim]@ {ev.location}[/dim]"
        console.print(line)


@click.group(name="calendar")
def calendar_group() -> None:
    """Read-only calendar integration via ICS feed URL.

    Source: a "secret address in iCal format" URL from your provider.
    Stash via:
        <product> secrets set calendar.ics_url
        <product> settings set calendar.enabled=true
    """


@calendar_group.command(name="today")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
)
def calendar_today(config_path):
    """Show today's calendar events (UTC midnight to next-midnight)."""
    from agent_core.secrets import default_store
    from agent_core.settings import SettingsManager
    from agent_core.work.calendar import (
        CalendarFetcher,
        CalendarFetchError,
        fetch_today,
    )

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    try:
        fetcher = CalendarFetcher.from_settings(mgr.settings, default_store())
    except CalendarFetchError as e:
        console.print(f"[red]calendar not configured:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    events = fetch_today(fetcher)
    _print_calendar_events(events)


@calendar_group.command(name="upcoming")
@click.option(
    "--hours",
    type=int,
    default=24,
    show_default=True,
    help="How far ahead to look. 24 = next day, 168 = next week.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
)
def calendar_upcoming(hours, config_path):
    """Show events in the next ``--hours`` from now."""
    from agent_core.secrets import default_store
    from agent_core.settings import SettingsManager
    from agent_core.work.calendar import (
        CalendarFetcher,
        CalendarFetchError,
        fetch_window,
    )

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    try:
        fetcher = CalendarFetcher.from_settings(mgr.settings, default_store())
    except CalendarFetchError as e:
        console.print(f"[red]calendar not configured:[/red] {e}")
        raise click.exceptions.Exit(1) from e

    events = fetch_window(fetcher, hours=hours)
    _print_calendar_events(events)


__all__ = ["calendar_group"]
