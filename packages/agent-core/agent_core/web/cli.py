"""``agent serve`` — start the agent_core.web FastAPI server.

Reads the bearer token from the secrets store (set during ``init``) and
starts uvicorn on the configured host/port. Prints the OpenAPI docs URL +
the token (so the user can paste it into the OpenWebUI plugin).

The token comes from the secret namespace agent_core / web.api_token. If
none is found, refuses to start — fail closed (per the agent_core.web
design principle that an unconfigured app must NOT silently accept all
callers).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console

from agent_core.ops.cli import API_TOKEN_KEY, SECRETS_NAMESPACE
from agent_core.secrets import default_store
from agent_core.settings import SettingsManager
from agent_core.state.db import Database
from agent_core.web import create_app

console = Console()


@click.command(name="serve")
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
    help="SQLAlchemy URL for the agent database. Defaults to settings.storage.url.",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--port", default=8765, show_default=True, type=int, help="Bind port.")
@click.option(
    "--token",
    "api_token",
    default=None,
    help=(
        "Override the API token (default: read from secrets store). Useful "
        "when the secrets backend is not available (CI, ephemeral containers)."
    ),
)
@click.option(
    "--reload",
    is_flag=True,
    help="Auto-reload on code changes (development only).",
)
def serve_command(
    config_path: Path | None,
    db_url: str | None,
    host: str,
    port: int,
    api_token: str | None,
    reload: bool,
) -> None:
    """Start the agent_core.web FastAPI server.

    Pre-reqs (one-time): run ``init`` first to bootstrap the schema and
    generate an API token. Then ``serve`` starts the API. Tokens are
    pulled from the secrets store automatically.
    """
    try:
        mgr = SettingsManager(path=config_path)
    except Exception as e:
        console.print(f"[red]could not load settings:[/red] {e}")
        sys.exit(1)

    resolved_url = db_url or mgr.get("storage.url")
    if not resolved_url:
        console.print("[red]no db url:[/red] pass --db-url or set storage.url")
        sys.exit(1)

    # Resolve token: explicit --token wins; else secrets store; else fail.
    if api_token is None:
        api_token = default_store().get(SECRETS_NAMESPACE, API_TOKEN_KEY)
    if not api_token:
        console.print(
            "[red]no API token configured:[/red] run [cyan]init[/cyan] first "
            "(or pass --token explicitly)."
        )
        sys.exit(1)

    db = Database(resolved_url)
    app = create_app(db, mgr, api_tokens={api_token})

    base_url = f"http://{host}:{port}"
    console.print(f"[green]starting agent_core.web[/green] on [cyan]{base_url}[/cyan]")
    console.print(f"  OpenAPI docs:   {base_url}/docs")
    console.print(f"  Health check:   {base_url}/health")
    console.print(f"  ObligationBoard endpoint: {base_url}/obligations")
    console.print()
    console.print("[bold]API token (paste into OpenWebUI plugin):[/bold]")
    console.print(f"  {api_token}")
    console.print()
    console.print("[dim]Ctrl-C to stop.[/dim]")

    # Lazy import — uvicorn pulls in a lot.
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


__all__ = ["serve_command"]
