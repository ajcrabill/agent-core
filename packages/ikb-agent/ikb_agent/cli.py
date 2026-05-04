"""ikb-agent CLI — ``ikb <command>``.

Wraps agent-core's command groups (``settings``, ``ops``) and adds ikb-
specific defaults (Postgres DSN via env, "team KB" framing in --help).

Top-level commands:

    ikb settings show / set / reset / preset / path / doctor
    ikb doctor
    ikb backup / restore
    ikb setup --tier 1|2|3
    ikb info     (ikb-specific: print resolved DSN + paths + versions)
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agent_core.ops.cli import (
    backup_command,
    doctor_command,
    init_command,
    restore_command,
    setup_command,
)
from agent_core.migrations.cli import migrate_group
from agent_core.ops.secrets_cli import secrets_group
from agent_core.settings.cli import settings_group
from agent_core.web.cli import serve_command

from ikb_agent import __version__
from ikb_agent.defaults import (
    INSTANCE_NAME,
    config_dir,
    default_db_url,
    default_settings_path,
    state_dir,
)

console = Console()


@click.group(
    name="ikb",
    help=(
        "ikb-agent — your team's intelligent knowledge base. PostgreSQL-backed,\n"
        "mesh-enabled, built on agent-core. Run `ikb info` to see your config,\n"
        "or `ikb setup` for the guided install wizard."
    ),
)
@click.version_option(__version__)
def cli() -> None:
    """Top-level ikb command."""


cli.add_command(settings_group, name="settings")
cli.add_command(secrets_group, name="secrets")
cli.add_command(migrate_group, name="migrate")


@cli.command(name="doctor")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="ikb config dir",
    help="Path to agent.yml.",
)
@click.option(
    "--db-url",
    default=None,
    show_default="reads from settings.storage.url",
    help="SQLAlchemy URL for the agent database. Override settings.storage.url.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.pass_context
def doctor(ctx, config_path, db_url, as_json):
    """Run health checks against the ikb install."""
    ctx.invoke(doctor_command, config_path=config_path, db_url=db_url, as_json=as_json)


@cli.command(name="backup")
@click.argument("output", type=click.Path(path_type=Path))
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="ikb config",
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
    show_default="env IKB_DB_URL or local socket",
)
@click.pass_context
def backup(ctx, output, config_path, db_url):
    """Snapshot ikb state to a portable JSON file."""
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
    show_default="env IKB_DB_URL or local socket",
)
@click.option(
    "--settings-path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="ikb config",
    help="Where to write the embedded agent.yml (if any).",
)
@click.option("--yes", is_flag=True)
@click.option("--skip-schema-check", is_flag=True)
@click.pass_context
def restore(ctx, source, db_url, settings_path, yes, skip_schema_check):
    """Restore ikb state from a backup file (DESTRUCTIVE)."""
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
    show_default="ikb config dir",
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
    show_default="env IKB_DB_URL or local socket",
    help="SQLAlchemy URL forwarded to init (default: ikb postgres DSN).",
)
@click.option("--no-init", is_flag=True)
@click.option("--no-doctor", is_flag=True)
@click.pass_context
def setup(ctx, tier, config_path, db_url, no_init, no_doctor):
    """Interactive setup wizard. Runs init + doctor at the end by default."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_path = state_dir() / "agent.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.invoke(
        setup_command,
        tier=tier,
        config_path=config_path,
        db_url=db_url,
        no_init=no_init,
        no_doctor=no_doctor,
        default_db_urls={
            "sqlite": f"sqlite:///{sqlite_path}",
            "postgres": default_db_url(),
        },
    )


@cli.command(name="init")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=lambda: default_settings_path(),
    show_default="ikb config dir",
)
@click.option(
    "--db-url",
    default=None,
    show_default="reads from settings.storage.url",
    help=(
        "SQLAlchemy URL to bootstrap the schema against. When omitted, "
        "init reads settings.storage.url (which the wizard wrote for you). "
        "Use this to override for a one-shot init against a different DB."
    ),
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
    show_default="ikb config dir",
)
@click.option(
    "--db-url",
    default=lambda: default_db_url(),
    show_default="env IKB_DB_URL or local socket",
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


@cli.command(name="info")
def info() -> None:
    """Show resolved ikb paths + DSN + version. Useful in bug reports."""
    table = Table(title="ikb-agent", show_header=False)
    table.add_column("key", style="cyan", no_wrap=True)
    table.add_column("value")
    table.add_row("version", __version__)
    table.add_row("instance", INSTANCE_NAME)
    table.add_row("config dir", str(config_dir()))
    table.add_row("settings file", str(default_settings_path()))
    table.add_row("state dir", str(state_dir()))
    table.add_row("db url", _redact_password(default_db_url()))
    table.add_row(
        "config exists",
        "yes" if default_settings_path().exists() else "no (run `ikb setup`)",
    )
    console.print(table)


def _redact_password(url: str) -> str:
    """Hide the password component of a DSN — `info` output may end up in
    bug reports / Slack screenshots."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


def main() -> None:
    """ikb-agent entry point.

    Sets ``AGENT_DATA_DIR`` so the agent-core ``settings`` group (borrowed
    via ``cli.add_command``) resolves its default ``--config`` path to
    ``~/.config/ikb-agent/agent.yml`` instead of ``cwd/agent.yml``. Without
    this, ``ikb settings set foo=bar`` would write to whatever directory
    the user happened to run from. Power users overriding AGENT_DATA_DIR
    stay in control (setdefault).
    """
    import os

    os.environ.setdefault("AGENT_DATA_DIR", str(config_dir()))
    cli()


if __name__ == "__main__":
    main()
