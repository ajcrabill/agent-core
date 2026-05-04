"""Click CLI for ``agent doctor / backup / restore / setup``.

Mirrors the surface area of ``agent settings`` (which lives in
``agent_core.settings.cli``). The two are mounted into a single ``agent``
command in ``agent_core.cli`` (when that lands).

Each command takes ``--config`` / ``--db-url`` so it can be pointed at a
non-default install. Defaults match what ``SettingsManager`` would resolve
on a fresh box.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agent_core.ops.backup import (
    BackupFormatError,
    create_backup,
    read_backup,
    write_backup,
)
from agent_core.ops.doctor import CheckStatus, Doctor, DoctorContext
from agent_core.ops.restore import (
    RestoreError,
    RestoreNotConfirmedError,
    RestoreSchemaMismatchError,
    restore_backup,
)
from agent_core.ops.wizard import SetupWizard, WizardValidationError
from agent_core.settings import SettingsManager
from agent_core.state.db import Database

console = Console()


# ── doctor ──────────────────────────────────────────────────────────────────


@click.command(name="doctor")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to agent.yml (default: env or cwd).",
)
@click.option(
    "--db-url",
    default=None,
    help=(
        "SQLAlchemy URL for the agent database. When omitted, reads "
        "settings.storage.url (same as init). Set to '' to force-skip db checks."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def doctor_command(config_path: Path | None, db_url: str | None, as_json: bool) -> None:
    """Run health checks against the install. Exits non-zero on any fail."""
    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        sys.exit(1)
    resolved_url = db_url if db_url is not None else mgr.get("storage.url")
    db = Database(resolved_url) if resolved_url else None
    ctx = DoctorContext(settings=mgr, db=db)
    report = Doctor().run(ctx)

    if as_json:
        out = [
            {"name": r.name, "status": r.status.value, "message": r.message, "details": r.details}
            for r in report.results
        ]
        click.echo(json.dumps(out, indent=2))
    else:
        table = Table(title="agent doctor")
        table.add_column("check", style="cyan", no_wrap=True)
        table.add_column("status", no_wrap=True)
        table.add_column("message")
        for r in report.results:
            color = {
                CheckStatus.ok: "green",
                CheckStatus.warn: "yellow",
                CheckStatus.fail: "red",
                CheckStatus.skipped: "dim",
            }[r.status]
            table.add_row(r.name, f"[{color}]{r.status.value}[/{color}]", r.message)
        console.print(table)
        counts = report.by_status()
        summary_parts = [f"{counts[s]} {s.value}" for s in CheckStatus if counts[s]]
        console.print(f"[dim]{', '.join(summary_parts)}[/dim]")

    sys.exit(0 if report.ok else 1)


# ── backup ──────────────────────────────────────────────────────────────────


@click.command(name="backup")
@click.argument("output", type=click.Path(path_type=Path))
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to agent.yml (will be embedded in the backup).",
)
@click.option(
    "--db-url", default=None, help="SQLAlchemy URL for the agent database (required)."
)
@click.option(
    "--include-identity-public-key",
    default=None,
    help="Embed the agent's public identity key. Optional; never includes secrets.",
)
def backup_command(
    output: Path,
    config_path: Path | None,
    db_url: str | None,
    include_identity_public_key: str | None,
) -> None:
    """Write a portable JSON backup of the agent's state to OUTPUT."""
    if not db_url:
        console.print("[red]--db-url is required[/red]")
        sys.exit(2)
    db = Database(db_url)

    settings_path = config_path
    if settings_path is None:
        try:
            settings_path = SettingsManager().path
        except Exception:
            settings_path = None

    payload = create_backup(
        db,
        settings_path=settings_path if settings_path and settings_path.exists() else None,
        include_identity=bool(include_identity_public_key),
        identity_public_key=include_identity_public_key,
    )
    write_backup(payload, output)
    counts = payload["manifest"]["tables"]
    total = sum(counts.values())
    console.print(
        f"[green]wrote backup[/green] {output} "
        f"({len(counts)} tables, {total} rows, {output.stat().st_size:,} bytes)"
    )


# ── restore ─────────────────────────────────────────────────────────────────


@click.command(name="restore")
@click.argument("source", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--db-url",
    default=None,
    help="SQLAlchemy URL for the target database (required).",
)
@click.option(
    "--settings-path",
    type=click.Path(path_type=Path),
    default=None,
    help="If the backup carries settings_yaml, write it here.",
)
@click.option("--yes", is_flag=True, help="Skip the destructive confirmation prompt.")
@click.option(
    "--skip-schema-check",
    is_flag=True,
    help="Restore even if backup schema_head differs from current.",
)
def restore_command(
    source: Path,
    db_url: str | None,
    settings_path: Path | None,
    yes: bool,
    skip_schema_check: bool,
) -> None:
    """Restore agent state from a backup file (DESTRUCTIVE)."""
    if not db_url:
        console.print("[red]--db-url is required[/red]")
        sys.exit(2)

    try:
        payload = read_backup(source)
    except BackupFormatError as e:
        console.print(f"[red]bad backup file:[/red] {e}")
        sys.exit(1)

    counts = payload["manifest"]["tables"]
    total = sum(counts.values())
    console.print(
        f"about to overwrite the target db with [bold]{total}[/bold] rows across "
        f"[bold]{len(counts)}[/bold] tables from {source}"
    )

    if not yes:
        click.confirm("Proceed?", abort=True)

    db = Database(db_url)
    try:
        report = restore_backup(
            db,
            payload,
            confirm=True,
            settings_path=settings_path,
            skip_schema_check=skip_schema_check,
        )
    except RestoreSchemaMismatchError as e:
        console.print(f"[red]schema mismatch:[/red] {e}")
        console.print("[dim]Pass --skip-schema-check to override.[/dim]")
        sys.exit(1)
    except (RestoreError, RestoreNotConfirmedError) as e:
        console.print(f"[red]restore failed:[/red] {e}")
        sys.exit(1)

    inserted = sum(report.rows_inserted.values())
    console.print(f"[green]restored[/green] {inserted} rows into {len(report.rows_inserted)} tables")
    if report.skipped_tables:
        console.print(
            f"[yellow]skipped[/yellow] {len(report.skipped_tables)} tables "
            f"not present in current schema: {report.skipped_tables}"
        )


# ── setup wizard ────────────────────────────────────────────────────────────


@click.command(name="setup")
@click.option(
    "--tier",
    type=click.IntRange(1, 3),
    default=1,
    help="1 = minimum viable; 2 = + integrations + push; 3 = every knob.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write agent.yml (default: env or cwd).",
)
@click.option(
    "--no-init",
    is_flag=True,
    help="Skip the schema bootstrap + token generation that normally follow setup.",
)
@click.option(
    "--no-doctor",
    is_flag=True,
    help="Skip the doctor health-check that normally follows init.",
)
@click.option(
    "--db-url",
    default=None,
    help="SQLAlchemy URL for the agent database (passed to init).",
)
@click.pass_context
def setup_command(
    ctx: click.Context,
    tier: int,
    config_path: Path | None,
    no_init: bool,
    no_doctor: bool,
    db_url: str | None,
    default_db_urls: dict[str, str] | None = None,
) -> None:
    """Interactive setup wizard. Runs init + doctor at the end by default.

    The full first-run flow is: ask 3 questions, write agent.yml, bootstrap
    the schema, generate an API token, run health checks. Pass --no-init
    or --no-doctor to skip those tail steps; useful in CI or when scripting
    around the wizard.

    ``default_db_urls`` is the product's per-backend URL preference (e.g.
    ``{"sqlite": "sqlite:///<state>/agent.db", "postgres": "<dsn>"}``).
    The wizard uses it to write a sensible storage.url after the user
    picks a backend, so subsequent ``init`` reads a consistent URL out
    of settings instead of falling back to the schema default
    ``sqlite:///./agent.db`` (which is cwd-relative — almost never what
    the user wants on the second run from a different directory).
    """
    try:
        result = SetupWizard(default_db_urls=default_db_urls).run(tier=tier)  # type: ignore[arg-type]
    except WizardValidationError as e:
        console.print(f"[red]validation failed:[/red] {e}")
        sys.exit(1)

    target = config_path or SettingsManager().path
    target.parent.mkdir(parents=True, exist_ok=True)
    result.commit(target)
    console.print(f"[green]wrote settings to[/green] {target}")
    if result.overrides.get("__display_name"):
        console.print(
            f"[dim]display name {result.overrides['__display_name']!r} captured.[/dim]"
        )

    if no_init:
        console.print(
            "[dim]skipped init (--no-init). Run [cyan]init[/cyan] manually before [cyan]serve[/cyan].[/dim]"
        )
        return

    console.print()
    ctx.invoke(init_command, config_path=target, db_url=db_url, rotate_token=False)

    if no_doctor:
        return

    console.print()
    ctx.invoke(doctor_command, config_path=target, db_url=db_url, as_json=False)


# ── init: bootstrap schema + generate API token ──────────────────────────


SECRETS_NAMESPACE = "agent_core"
"""Namespace for agent-core's own secrets (web API token, etc.)."""

API_TOKEN_KEY = "web.api_token"
"""Secret key for the bearer token agent_core.web (and OpenWebUI plugin) use."""


@click.command(name="init")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to agent.yml (default: env/cwd; used to discover db_url).",
)
@click.option(
    "--db-url",
    default=None,
    help=(
        "SQLAlchemy URL for the agent database. If omitted, reads from "
        "settings.storage.url."
    ),
)
@click.option(
    "--rotate-token",
    is_flag=True,
    help="Force-generate a new API token even if one already exists.",
)
@click.option(
    "--llm-provider",
    type=click.Choice(["stub", "openai_compat", "ollama"]),
    default=None,
    help="Configure the LLM provider in one shot. Writes settings.llm.provider.",
)
@click.option(
    "--llm-base-url",
    default=None,
    help="LLM endpoint URL (only used with --llm-provider). Common values: "
    "https://api.openai.com/v1, https://api.deepseek.com/v1, "
    "http://localhost:11434/v1 (Ollama).",
)
@click.option(
    "--llm-model",
    default=None,
    help="Model name (only used with --llm-provider).",
)
@click.option(
    "--llm-api-key",
    default=None,
    help=(
        "API key for the LLM (stored in the secrets store under llm/<key>). "
        "Read from stdin if value is '-'."
    ),
)
def init_command(
    config_path: Path | None,
    db_url: str | None,
    rotate_token: bool,
    llm_provider: str | None,
    llm_base_url: str | None,
    llm_model: str | None,
    llm_api_key: str | None,
) -> None:
    """Bootstrap a fresh install: create the schema + generate an API token.

    Optionally configures the LLM provider in the same step (so a single
    ``init --llm-provider openai_compat --llm-api-key sk-...`` lands a
    fully-configured install).

    Idempotent — calling twice on an already-initialized install is safe
    (schema is a no-op when present; token rotation is opt-in via
    ``--rotate-token``).
    """
    import secrets as _secrets

    from agent_core.secrets import default_store
    from agent_core.state.db import Database

    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        sys.exit(1)

    resolved_url = db_url or mgr.get("storage.url")
    if not resolved_url:
        console.print(
            "[red]no db url:[/red] pass --db-url or set storage.url in agent.yml"
        )
        sys.exit(1)

    # Bootstrap schema via alembic — both creates the schema AND stamps
    # alembic_version so future `alembic upgrade head` runs cleanly.
    try:
        _alembic_upgrade_head(resolved_url)
    except Exception as e:
        console.print(f"[red]schema bootstrap failed:[/red] {e}")
        sys.exit(1)
    console.print(f"[green]schema at head[/green] ({resolved_url})")

    # Generate / load API token.
    store = default_store()
    existing = store.get(SECRETS_NAMESPACE, API_TOKEN_KEY)
    if existing and not rotate_token:
        console.print(
            "[dim]API token already present in secrets store; "
            "pass --rotate-token to replace it.[/dim]"
        )
        token = existing
    else:
        token = _secrets.token_urlsafe(32)
        try:
            store.set(SECRETS_NAMESPACE, API_TOKEN_KEY, token)
        except Exception as e:
            console.print(f"[yellow]could not store token:[/yellow] {e}")
            console.print(
                f"[yellow]save manually:[/yellow] AGENTCORE_AGENT_CORE_WEB_API_TOKEN={token}"
            )

    console.print()
    console.print("[bold]API token (paste into your OpenWebUI plugin):[/bold]")
    console.print(f"  {token}")

    # Optional one-shot LLM configuration. We do this AFTER the token write
    # so a partial failure here doesn't leave the install token-less.
    if llm_provider is not None:
        _configure_llm(
            mgr=mgr,
            store=store,
            provider=llm_provider,
            base_url=llm_base_url,
            model=llm_model,
            api_key=llm_api_key,
        )

    console.print()
    console.print(
        "[dim]next:[/dim] run [cyan]doctor[/cyan] to verify, then [cyan]serve[/cyan] to start the API."
    )


# ── Group ───────────────────────────────────────────────────────────────────


@click.group(name="ops")
def ops_group() -> None:
    """doctor / backup / restore / setup / init — operational commands."""


ops_group.add_command(doctor_command)
ops_group.add_command(backup_command)
ops_group.add_command(restore_command)
ops_group.add_command(setup_command)
ops_group.add_command(init_command)


# ── Helpers ───────────────────────────────────────────────────────────────


def _configure_llm(
    *,
    mgr: SettingsManager,
    store,
    provider: str,
    base_url: str | None,
    model: str | None,
    api_key: str | None,
) -> None:
    """Land an LLM config in one call: writes settings.llm.* + stores the
    API key under namespace 'llm'. Used by ``init --llm-provider``.

    Resolves sensible defaults per provider so a bare
    ``--llm-provider openai_compat --llm-api-key sk-...`` is enough.
    """
    # Provider-aware defaults
    if provider == "ollama":
        default_base_url = "http://localhost:11434/v1"
        default_model = "llama3.2"
        default_key_name = "ollama_api_key"
    elif provider == "openai_compat":
        default_base_url = "https://api.openai.com/v1"
        default_model = "gpt-4o-mini"
        default_key_name = "openai_api_key"
    else:  # stub
        default_base_url = "https://api.openai.com/v1"
        default_model = "stub"
        default_key_name = "openai_api_key"

    base_url = base_url or default_base_url
    model = model or default_model

    # Save on every call — each set() re-reads the file fresh, so
    # save=False would discard prior values when the next call reads.
    mgr.set("llm.provider", provider)
    mgr.set("llm.base_url", base_url)
    mgr.set("llm.model", model)
    mgr.set("llm.api_key_secret_key", default_key_name)

    # Read api key from stdin if "-"
    if api_key == "-":
        import sys as _sys

        api_key = _sys.stdin.read().strip()

    if api_key:
        try:
            store.set("llm", default_key_name, api_key)
            console.print(
                f"[green]LLM configured[/green] provider={provider} model={model}"
            )
        except Exception as e:
            console.print(f"[yellow]LLM key not stored:[/yellow] {e}")
            console.print(
                f"  set manually: [cyan]AGENTCORE_LLM_{default_key_name.upper()}=...[/cyan]"
            )
    else:
        console.print(
            f"[yellow]LLM provider={provider} configured but no API key set.[/yellow] "
            f"Pass [cyan]--llm-api-key sk-...[/cyan] or set "
            f"[cyan]AGENTCORE_LLM_{default_key_name.upper()}=...[/cyan]"
        )


def _alembic_upgrade_head(db_url: str) -> None:
    """Run ``alembic upgrade head`` against ``db_url`` using the bundled
    migration script directory. Idempotent — no-op when already at head.

    Lives here (not in agent_core.state.db) because it's the install-time
    operation, not a runtime concern. Database.create_all() stays for tests
    where alembic overhead isn't worth it."""
    from importlib.resources import files

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(files("agent_core.state.migrations")))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def main() -> None:
    ops_group()


if __name__ == "__main__":
    main()
