"""Shared `secrets` CLI group — used by both dcos-agent and ikb-agent.

Mount via ``cli.add_command(secrets_group, name="secrets")`` in your
product CLI. The group itself is product-agnostic — it just wraps the
shared ``agent_core.secrets.default_store()`` resolution.

Surface:
    secrets set <namespace.key>[=<value>]    # value via flag / stdin / prompt
    secrets get <namespace.key> [--show]     # redacted by default
    secrets delete <namespace.key> [--yes]
    secrets list [<namespace>]               # known keys; no values
"""

from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.table import Table

console = Console()


def _split_secret_path(dotted: str) -> tuple[str, str]:
    """Split ``namespace.key`` into (namespace, key). Reject invalid forms."""
    if "." not in dotted:
        raise click.UsageError(
            f"expected '<namespace>.<key>', got {dotted!r}. "
            "Examples: llm.openai_api_key, email.imap_password"
        )
    namespace, key = dotted.split(".", 1)
    if not namespace or not key:
        raise click.UsageError(f"both namespace and key are required, got {dotted!r}")
    return namespace, key


@click.group(name="secrets")
def secrets_group() -> None:
    """Manage secrets (API keys, IMAP passwords, etc).

    Backed by the OS keychain on macOS / Windows / Linux-with-Secret-Service,
    or a 0600-mode JSON file at ~/.local/state/agent-core/secrets.json on
    headless Linux (and on macOS-over-SSH, where the keychain rejects
    writes from non-GUI sessions).

    Secrets live under namespaces — common ones:

    \b
      llm.openai_api_key       OpenAI / OpenAI-compat bearer
      llm.deepseek_api_key     DeepSeek bearer
      email.imap_password      IMAP / Gmail app password
      email.smtp_password      SMTP password (often same as IMAP for Gmail)
      calendar.ics_url         ICS feed URL ("secret address in iCal format")
      agent_core.web.api_token API token managed by `init`
    """


@secrets_group.command(name="set")
@click.argument("assignment", required=False, default=None)
@click.option(
    "--from-stdin",
    is_flag=True,
    help=(
        "Read the value from stdin instead of the assignment / interactive "
        "prompt. Useful for piping: "
        "`cat token | <product> secrets set --from-stdin llm.openai_api_key`."
    ),
)
def secrets_set(assignment, from_stdin):
    """Store a secret in the OS keychain (or file fallback).

    \b
    Three input modes:
      <product> secrets set llm.openai_api_key=sk-...      # one-shot (visible in history)
      <product> secrets set llm.openai_api_key             # interactive prompt (recommended)
      cat token | <product> secrets set --from-stdin email.imap_password
    """
    from agent_core.secrets import default_store

    if assignment is None:
        raise click.UsageError(
            "specify the secret as `<namespace>.<key>[=<value>]`. "
            "See `secrets set --help` for input modes."
        )

    if "=" in assignment:
        dotted, value = assignment.split("=", 1)
    elif from_stdin:
        dotted = assignment
        value = sys.stdin.read().strip()
        if not value:
            console.print("[red]stdin was empty; no secret set.[/red]")
            raise click.exceptions.Exit(1)
    else:
        dotted = assignment
        value = click.prompt(
            f"value for {dotted}",
            hide_input=True,
            confirmation_prompt=True,
        )

    namespace, key = _split_secret_path(dotted)
    store = default_store()
    store.set(namespace, key, value)
    console.print(f"[green]✓[/green] stored {namespace}.{key} ([dim]{type(store).__name__}[/dim])")


@secrets_group.command(name="get")
@click.argument("dotted")
@click.option(
    "--show",
    is_flag=True,
    help="Print the actual value. Default redacts to [REDACTED] for shell-history safety.",
)
def secrets_get(dotted, show):
    """Look up a secret. Redacted by default — pass --show to reveal."""
    from agent_core.secrets import default_store

    namespace, key = _split_secret_path(dotted)
    store = default_store()
    value = store.get(namespace, key)
    if value is None:
        console.print(f"[yellow]not set:[/yellow] {namespace}.{key}")
        raise click.exceptions.Exit(2)
    if show:
        click.echo(value)
    else:
        console.print(f"{namespace}.{key} = [dim][REDACTED, len={len(value)}][/dim]")


@secrets_group.command(name="delete")
@click.argument("dotted")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def secrets_delete(dotted, yes):
    """Remove a secret from the store. Cannot be undone."""
    from agent_core.secrets import default_store

    namespace, key = _split_secret_path(dotted)
    if not yes and not click.confirm(f"delete {namespace}.{key}?"):
        console.print("[dim]aborted[/dim]")
        return
    store = default_store()
    store.delete(namespace, key)
    console.print(f"[green]✓[/green] deleted {namespace}.{key}")


@secrets_group.command(name="list")
@click.argument("namespace", required=False, default=None)
def secrets_list(namespace):
    """List secret keys under a namespace (no values).

    \b
      <product> secrets list             # show all known namespaces with keys
      <product> secrets list llm         # list keys under 'llm' only
    """
    from agent_core.secrets import default_store

    store = default_store()
    if namespace:
        keys = store.list(namespace)
        if not keys:
            console.print(f"[dim]no keys under namespace {namespace!r}[/dim]")
            return
        for k in sorted(keys):
            console.print(f"  {namespace}.{k}")
        return

    # No namespace given — probe a few well-known ones.
    known = ["llm", "email", "calendar", "agent_core"]
    table = Table(title=f"secrets ({type(store).__name__})")
    table.add_column("namespace", style="cyan")
    table.add_column("keys", style="dim")
    any_rows = False
    for ns in known:
        try:
            keys = store.list(ns)
        except Exception:
            keys = []
        if keys:
            table.add_row(ns, ", ".join(sorted(keys)))
            any_rows = True
    if not any_rows:
        console.print("[dim]no secrets stored under known namespaces[/dim]")
    else:
        console.print(table)


__all__ = ["secrets_group"]
