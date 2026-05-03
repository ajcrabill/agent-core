"""ikb-agent product defaults.

Team Intelligent Knowledge Base defaults: PostgreSQL-backed (pgvector
optional for native vector search), mesh-enabled by default (small team
sharing the install), balanced preset, daily digest on.

Where things live (paths cover the local-driver case; the database itself
lives wherever the DSN points):
    state    : $XDG_STATE_HOME / ikb-agent /
    config   : $XDG_CONFIG_HOME / ikb-agent / agent.yml
"""

from __future__ import annotations

import os
from pathlib import Path

INSTANCE_NAME = "ikb-agent"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / INSTANCE_NAME


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / INSTANCE_NAME


def default_settings_path() -> Path:
    return config_dir() / "agent.yml"


def default_db_url() -> str:
    """Default Postgres DSN. Overridable via the ``IKB_DB_URL`` env var.

    Convention: local Unix socket on ``/tmp`` (matches agent-core's
    default_postgres_dsn behavior). Production deployments usually point
    this at a managed Postgres via env var or the wizard."""
    if env := os.environ.get("IKB_DB_URL"):
        return env
    return "postgresql+psycopg:///ikb_agent?host=/tmp"


__all__ = [
    "INSTANCE_NAME",
    "config_dir",
    "default_db_url",
    "default_settings_path",
    "state_dir",
]
