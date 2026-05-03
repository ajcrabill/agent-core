"""``agent migrate`` CLI — one-shot data conversions into backup-format JSON.

Each subcommand takes a source location and writes a ``backup.json`` the
existing ``ops restore`` consumes. Two-step workflow stays the same:

    agent migrate from-loriah-vault ~/Documents/Obsidian\\ Vault \\
                                    --output ~/migrations/loriah.json
    dcos restore ~/migrations/loriah.json --db-url <target-db-url>

Mounted into the dcos-agent / ikb-agent CLIs in their own files; standalone
for ``python -m agent_core.migrations.cli``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agent_core.migrations.from_esby_install import (
    migrate_esby_install,
    to_backup_payload as esby_to_backup_payload,
)
from agent_core.migrations.from_loriah_vault import (
    DEFAULT_VAULT_PATHS,
    migrate_loriah_vault,
    to_backup_payload,
)
from agent_core.ops.backup import write_backup

console = Console()


@click.group(name="migrate")
def migrate_group() -> None:
    """One-shot migrations from legacy formats to agent-core."""


@migrate_group.command(name="from-loriah-vault")
@click.argument("vault_path", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(path_type=Path),
    required=True,
    help="Where to write the backup JSON.",
)
@click.option(
    "--preset",
    type=click.Choice(["cautious", "balanced", "aggressive"]),
    default="balanced",
    show_default=True,
    help="AgentSettings preset to embed in the migration.",
)
@click.option(
    "--no-seed-obligations",
    is_flag=True,
    help="Skip the curated seed Obligations; only import Thoughts.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be migrated without writing the JSON.",
)
def from_loriah_vault(
    vault_path: Path,
    output_path: Path,
    preset: str,
    no_seed_obligations: bool,
    dry_run: bool,
) -> None:
    """Read Loriah's Obsidian vault; produce a backup JSON for restore_backup."""
    state = migrate_loriah_vault(
        vault_path,
        settings_preset=preset,
        include_seed_obligations=not no_seed_obligations,
    )

    # Always show what was found
    table = Table(title=f"Loriah vault migration ({vault_path.name})")
    table.add_column("category", style="cyan")
    table.add_column("count", style="green", justify="right")
    table.add_row("Thoughts (markdown sections)", str(len(state.thoughts)))
    table.add_row("Sources (provenance rows)", str(len(state.sources)))
    table.add_row("Obligations (seeded)", str(len(state.obligations)))
    table.add_row("Files skipped (missing)", str(len(state.skipped_files)))
    console.print(table)

    if state.skipped_files:
        console.print(
            f"[yellow]heads-up:[/yellow] {len(state.skipped_files)} expected file(s) "
            f"not found in vault: {state.skipped_files}"
        )
        console.print(
            "  expected paths (relative to vault root):"
        )
        for label, rel in DEFAULT_VAULT_PATHS.items():
            tag = "✓" if rel not in state.skipped_files else "✗ missing"
            console.print(f"    {tag}  {rel}")

    if dry_run:
        console.print("[dim]--dry-run: payload not written.[/dim]")
        return

    payload = to_backup_payload(state)
    write_backup(payload, output_path)
    size = output_path.stat().st_size
    console.print(
        f"[green]wrote backup[/green] {output_path} ({size:,} bytes)"
    )
    console.print(
        "[dim]next:[/dim] dcos restore {p} --skip-schema-check [--yes]".format(p=output_path)
    )


@migrate_group.command(name="from-esby-install")
@click.argument(
    "install_root",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
)
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(path_type=Path),
    required=True,
    help="Where to write the backup JSON.",
)
@click.option(
    "--preset",
    type=click.Choice(["cautious", "balanced", "aggressive"]),
    default="balanced",
    show_default=True,
    help="AgentSettings preset (overridden by Esby's preferences.yaml if it sets autonomy_bias).",
)
@click.option(
    "--include-old-vault",
    is_flag=True,
    help="Also chunk markdown from ../.old EsbyVault/Esby/ into Thoughts.",
)
@click.option("--dry-run", is_flag=True, help="Show what would migrate without writing.")
def from_esby_install(
    install_root: Path,
    output_path: Path,
    preset: str,
    include_old_vault: bool,
    dry_run: bool,
) -> None:
    """Read Esby's installed-chief-of-staff dir; produce a backup JSON for restore_backup."""
    state = migrate_esby_install(
        install_root,
        settings_preset=preset,
        include_old_vault=include_old_vault,
    )

    table = Table(title=f"Esby install migration ({install_root.name})")
    table.add_column("category", style="cyan")
    table.add_column("count", style="green", justify="right")
    table.add_row("People (relationship CRM)", str(len(state.people)))
    table.add_row("LearningRules (translated policy_rules)", str(len(state.learning_rules)))
    table.add_row("Thoughts (configs + setup-report + old vault if any)", str(len(state.thoughts)))
    table.add_row("Sources (provenance rows)", str(len(state.sources)))
    table.add_row("Inputs skipped (missing)", str(len(state.skipped_inputs)))
    console.print(table)

    if state.skipped_inputs:
        console.print(
            f"[yellow]heads-up:[/yellow] {len(state.skipped_inputs)} expected input(s) "
            f"missing: {state.skipped_inputs}"
        )

    if dry_run:
        console.print("[dim]--dry-run: payload not written.[/dim]")
        return

    payload = esby_to_backup_payload(state)
    write_backup(payload, output_path)
    size = output_path.stat().st_size
    console.print(
        f"[green]wrote backup[/green] {output_path} ({size:,} bytes)"
    )
    console.print(
        "[dim]next:[/dim] ikb restore {p} --skip-schema-check [--yes]".format(p=output_path)
    )


def main() -> None:
    migrate_group()


if __name__ == "__main__":
    main()
