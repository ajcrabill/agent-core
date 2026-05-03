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
    restore_command,
    setup_command,
)
from agent_core.settings.cli import settings_group

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
@click.pass_context
def setup(ctx, tier, config_path):
    """Run the interactive setup wizard."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.invoke(setup_command, tier=tier, config_path=config_path)


# ── dcos-specific commands ────────────────────────────────────────────────


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


# ── Entry point ───────────────────────────────────────────────────────────


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
