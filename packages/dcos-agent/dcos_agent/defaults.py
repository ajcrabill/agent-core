"""dcos-agent product defaults.

Personal Chief of Staff defaults: SQLite-backed, single-user, balanced
preset, quiet-by-default notifications, vault optional.

Where things live:
    state    : $XDG_STATE_HOME / dcos-agent / agent.db
               (default: ~/.local/state/dcos-agent/agent.db)
    config   : $XDG_CONFIG_HOME / dcos-agent / agent.yml
               (default: ~/.config/dcos-agent/agent.yml)
"""

from __future__ import annotations

import os
from pathlib import Path

INSTANCE_NAME = "dcos-agent"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / INSTANCE_NAME


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / INSTANCE_NAME


def default_db_path() -> Path:
    return state_dir() / "agent.db"


def default_settings_path() -> Path:
    return config_dir() / "agent.yml"


def default_db_url() -> str:
    """SQLAlchemy URL for the default SQLite location."""
    return f"sqlite:///{default_db_path()}"


__all__ = [
    "INSTANCE_NAME",
    "config_dir",
    "default_db_path",
    "default_db_url",
    "default_settings_path",
    "state_dir",
]
