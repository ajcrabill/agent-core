"""Click CLI for ``agent settings``.

Usable as a standalone command (``python -m agent_core.settings.cli``) or
mounted into the main agent CLI as a sub-group:

    @click.group()
    def cli(): ...

    from agent_core.settings.cli import settings_group
    cli.add_command(settings_group)

Surface (mirrors what users will type into the wizard later):

    agent settings show                       # all values + sources
    agent settings show learning              # one section
    agent settings show learning.detector_strictness
    agent settings set autonomy.default_policy=cautious
    agent settings reset learning.detector_strictness
    agent settings reset                      # reset everything
    agent settings preset list
    agent settings preset show cautious       # diff from current
    agent settings preset apply cautious
    agent settings path                       # where agent.yml lives
    agent settings doctor                     # validate the file

Output styling uses ``rich`` (already a dep) for tables; falls back gracefully
to plain text for piped output."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agent_core.settings.manager import SettingsError, SettingsManager
from agent_core.settings.presets import PRESETS, apply_preset, list_presets
from agent_core.settings.schema import AgentSettings

console = Console()


# ── Group ──────────────────────────────────────────────────────────────────


@click.group(name="settings")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override path to agent.yml.",
)
@click.pass_context
def settings_group(ctx: click.Context, config_path: Path | None) -> None:
    """Manage agent settings: defaults → agent.yml → env vars."""
    try:
        ctx.obj = SettingsManager(path=config_path)
    except SettingsError as e:
        console.print(f"[red]settings error:[/red] {e}")
        sys.exit(1)


# ── show ────────────────────────────────────────────────────────────────────


@settings_group.command()
@click.argument("path", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.pass_obj
def show(mgr: SettingsManager, path: str | None, as_json: bool) -> None:
    """Show all settings (or one section / one key)."""
    rows = mgr.all_with_sources()
    if path:
        rows = [r for r in rows if r.path == path or r.path.startswith(path + ".")]
        if not rows:
            console.print(f"[yellow]no settings match {path!r}[/yellow]")
            sys.exit(2)

    if as_json:
        out = [{"path": r.path, "value": r.value, "source": r.source.value} for r in rows]
        click.echo(json.dumps(out, indent=2, default=str))
        return

    table = Table(title=f"agent settings ({mgr.path})")
    table.add_column("setting", style="cyan", no_wrap=True)
    table.add_column("value")
    table.add_column("source", style="dim")
    for r in rows:
        table.add_row(r.path, _render_value(r.value), r.source.value)
    console.print(table)


# ── set ─────────────────────────────────────────────────────────────────────


@settings_group.command(name="set")
@click.argument("assignment")
@click.pass_obj
def set_(mgr: SettingsManager, assignment: str) -> None:
    """Set a value: ``agent settings set autonomy.default_policy=cautious``."""
    if "=" not in assignment:
        console.print("[red]usage:[/red] settings set <dotted.path>=<value>")
        sys.exit(2)
    dotted, raw = assignment.split("=", 1)
    value = _coerce_value(raw)
    try:
        mgr.set(dotted, value)
    except (SettingsError, KeyError) as e:
        console.print(f"[red]rejected:[/red] {e}")
        sys.exit(1)
    console.print(f"[green]set[/green] {dotted} = {_render_value(value)}")


# ── reset ───────────────────────────────────────────────────────────────────


@settings_group.command()
@click.argument("path", required=False, default=None)
@click.option("--yes", is_flag=True, help="Skip confirmation when resetting all.")
@click.pass_obj
def reset(mgr: SettingsManager, path: str | None, yes: bool) -> None:
    """Reset a single key, or (with no PATH) every override."""
    if path is None:
        if not yes:
            click.confirm(
                "Reset ALL settings to schema defaults? agent.yml will be cleared.",
                abort=True,
            )
        mgr.reset()
        console.print("[green]all settings reset to defaults[/green]")
        return
    try:
        mgr.reset(path)
    except (SettingsError, KeyError) as e:
        console.print(f"[red]rejected:[/red] {e}")
        sys.exit(1)
    console.print(f"[green]reset[/green] {path}")


# ── preset ──────────────────────────────────────────────────────────────────


@settings_group.group(name="preset")
def preset_group() -> None:
    """Apply / inspect named setting presets."""


@preset_group.command(name="list")
def preset_list() -> None:
    """Show built-in preset names."""
    for name in list_presets():
        console.print(f"  • {name}")


@preset_group.command(name="show")
@click.argument("name")
@click.pass_obj
def preset_show(mgr: SettingsManager, name: str) -> None:
    """Show what would change if ``name`` were applied."""
    if name not in PRESETS:
        console.print(f"[red]unknown preset[/red] {name}; known: {list_presets()}")
        sys.exit(2)
    try:
        proposed = apply_preset(mgr.settings, name)  # type: ignore[arg-type]
    except Exception as e:
        console.print(f"[red]preset would fail validation:[/red] {e}")
        sys.exit(1)
    diffs = _diff_settings(mgr.settings, proposed)
    if not diffs:
        console.print(f"[dim]preset {name!r} matches current settings; nothing would change.[/dim]")
        return
    table = Table(title=f"preset {name!r} would change:")
    table.add_column("setting", style="cyan")
    table.add_column("current", style="dim")
    table.add_column("→")
    table.add_column("preset", style="green")
    for path, current, proposed_v in diffs:
        table.add_row(path, _render_value(current), "→", _render_value(proposed_v))
    console.print(table)


@preset_group.command(name="apply")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@click.pass_obj
def preset_apply(mgr: SettingsManager, name: str, yes: bool) -> None:
    """Apply a preset and persist to agent.yml."""
    if name not in PRESETS:
        console.print(f"[red]unknown preset[/red] {name}; known: {list_presets()}")
        sys.exit(2)
    if not yes:
        click.confirm(f"Apply preset {name!r} and write to {mgr.path}?", abort=True)
    try:
        mgr.apply_preset(name)  # type: ignore[arg-type]
    except (SettingsError, Exception) as e:
        console.print(f"[red]preset apply failed:[/red] {e}")
        sys.exit(1)
    console.print(f"[green]applied preset[/green] {name}")


# ── path ────────────────────────────────────────────────────────────────────


@settings_group.command()
@click.pass_obj
def path(mgr: SettingsManager) -> None:
    """Print where ``agent.yml`` lives (and whether it exists)."""
    exists = mgr.path.exists()
    console.print(f"{mgr.path}  [{'exists' if exists else 'not yet created'}]")


# ── doctor ──────────────────────────────────────────────────────────────────


@settings_group.command()
@click.pass_obj
def doctor(mgr: SettingsManager) -> None:
    """Validate agent.yml + env overlay; report any drift from defaults."""
    try:
        mgr.reload()
    except SettingsError as e:
        console.print(f"[red]invalid settings:[/red] {e}")
        sys.exit(1)
    overrides = [r for r in mgr.all_with_sources() if r.source.value != "default"]
    if not overrides:
        console.print("[green]ok[/green] (no overrides; all defaults)")
        return
    table = Table(title=f"{len(overrides)} override(s):")
    table.add_column("setting", style="cyan")
    table.add_column("value")
    table.add_column("source", style="dim")
    for r in overrides:
        table.add_row(r.path, _render_value(r.value), r.source.value)
    console.print(table)


# ── Helpers ────────────────────────────────────────────────────────────────


def _coerce_value(raw: str) -> object:
    """Convert a CLI string to a Python primitive. Same rules as env-var coercion."""
    lo = raw.strip().lower()
    if lo in ("true", "yes", "on"):
        return True
    if lo in ("false", "no", "off"):
        return False
    if lo in ("null", "none", ""):
        return None
    # Try JSON first (handles dicts, lists, escaped strings)
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    return raw


def _render_value(v: object) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)) and v:
        return json.dumps(v, default=str)
    if isinstance(v, (dict, list)):
        return "{}" if isinstance(v, dict) else "[]"
    return str(v)


def _diff_settings(
    current: AgentSettings,
    proposed: AgentSettings,
) -> list[tuple[str, object, object]]:
    """Return (path, current_value, proposed_value) for every leaf that differs."""
    out: list[tuple[str, object, object]] = []
    cur_d = current.model_dump()
    new_d = proposed.model_dump()
    for path, cur_v in _walk(cur_d):
        new_v = _get(new_d, path)
        if cur_v != new_v:
            out.append((path, cur_v, new_v))
    return out


def _walk(node: object, prefix: str = "") -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(_walk(v, f"{prefix}.{k}" if prefix else k))
    else:
        out.append((prefix, node))
    return out


def _get(d: dict, dotted: str) -> object:
    cur: object = d
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


# ── Standalone entry point ─────────────────────────────────────────────────


def main() -> None:
    settings_group()


if __name__ == "__main__":
    main()
