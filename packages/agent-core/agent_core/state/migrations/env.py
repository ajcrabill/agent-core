"""Alembic env.py — programmatic config, no .ini-driven URL.

agent-core ships migrations as part of the package. The Database class invokes
Alembic via `db.upgrade()`, passing the live URL so migrations apply against
whichever backend (sqlite or postgres) is in use.

Usage:
  from agent_core.state import Database
  db = Database.sqlite_memory()
  db.upgrade()                      # programmatic (preferred)
  # or:  uv run alembic upgrade head  (using the bundled alembic.ini for dev)
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# Import all models so SQLModel.metadata is fully populated before we use it.
from agent_core.state import models  # noqa: F401  (side-effect: registers tables)

# Alembic Config object provides access to .ini values when invoked via CLI.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate.
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emits SQL to stdout, no DB connection).

    Used by `alembic upgrade head --sql` to produce a script.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite-friendly: ALTER TABLE via batch ops
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against an actual database connection.

    Honors a pre-existing connection if injected via
    config.attributes['connection'] (used by `Database.upgrade()`); otherwise
    builds an engine from the .ini-driven URL.
    """
    connectable = config.attributes.get("connection", None)

    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            _do_run(connection)
    else:
        _do_run(connectable)


def _do_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
