"""Portable point-in-time export of agent state.

A backup is a single JSON file containing:

  - ``manifest``       — format version, schema head, table counts, timestamp
  - ``settings``       — copy of the resolved settings (the user's agent.yml
                          state, plus any env-var overrides at backup time)
  - ``tables``         — every SQLModel table dumped row-by-row (datetimes
                          serialized as ISO strings; JSON columns kept as-is)

JSON format (not tar) intentionally — user can ``cat``, ``diff``, ``jq`` it,
and store it in plain text in their existing backup tool. Cost: file size
grows with row count. For very large iKB-style installs we'll add a chunked
NDJSON variant later — not needed for personal-CoS scale.

Identity / secrets are NOT included by default. Reasoning:
    - Public identity is fine to back up but easy to recreate.
    - Private signing keys / API tokens are emphatically not safe to bundle
      into a JSON file the user might commit, share, or forward.

If you want identity in the backup, opt in explicitly with
``include_identity=True`` (this still excludes secret material — only the
public bundle goes in).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import inspect

from agent_core.state.db import Database

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass
class BackupManifest:
    """Header describing what's in a backup file."""

    format_version: int = FORMAT_VERSION
    agent_core_version: str = ""
    schema_head: str | None = None
    created_at: str = ""
    tables: dict[str, int] = field(default_factory=dict)
    includes_settings: bool = False
    includes_identity: bool = False


# ── Backup ──────────────────────────────────────────────────────────────────


def create_backup(
    db: Database,
    *,
    settings: object | None = None,
    settings_path: Path | None = None,
    include_identity: bool = False,
    identity_public_key: str | None = None,
) -> dict[str, Any]:
    """Build a backup payload (in-memory; caller writes to disk).

    Args:
        db: agent-core Database to dump.
        settings: Optional ``AgentSettings`` (or anything with ``model_dump()``).
            If provided, the resolved settings go into the backup.
        settings_path: If you'd rather embed the raw ``agent.yml``, pass its
            path. Wins over ``settings`` if both are provided.
        include_identity: If True, embed the agent's public identity bundle.
            Requires ``identity_public_key`` (kept explicit so we never reach
            into a SecretStore unprompted).

    Returns:
        Dict ready to JSON-serialize. Use ``write_backup()`` to atomically
        write to disk.
    """
    inspector = inspect(db.engine)
    table_names = sorted(inspector.get_table_names())

    tables_payload: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}

    for table in table_names:
        if table == "alembic_version":
            continue  # captured separately as schema_head
        rows = _dump_table(db, table)
        tables_payload[table] = rows
        counts[table] = len(rows)

    schema_head = _read_schema_head(db)

    manifest = BackupManifest(
        agent_core_version=_agent_core_version(),
        schema_head=schema_head,
        created_at=datetime.now(UTC).isoformat(),
        tables=counts,
        includes_settings=False,
        includes_identity=False,
    )

    payload: dict[str, Any] = {
        "manifest": manifest.__dict__,
        "tables": tables_payload,
    }

    if settings_path is not None and settings_path.exists():
        payload["settings_yaml"] = settings_path.read_text()
        manifest.includes_settings = True
    elif settings is not None and hasattr(settings, "model_dump"):
        payload["settings"] = settings.model_dump()
        manifest.includes_settings = True

    if include_identity:
        if not identity_public_key:
            raise ValueError(
                "include_identity=True requires identity_public_key — "
                "fetch it from your IdentityManager and pass explicitly"
            )
        payload["identity"] = {"public_key": identity_public_key}
        manifest.includes_identity = True

    payload["manifest"] = manifest.__dict__
    return payload


def write_backup(payload: dict[str, Any], path: Path) -> None:
    """Atomic write: serialize to a tempfile in the same dir, then rename."""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, indent=2, default=_json_fallback, sort_keys=True)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
    logger.info("wrote backup: %s (%d bytes)", path, path.stat().st_size)


def read_backup(path: Path) -> dict[str, Any]:
    """Load a backup file, validating that it looks like one."""
    payload = json.loads(path.read_text())
    if "manifest" not in payload:
        raise BackupFormatError(f"{path}: missing 'manifest' key")
    fmt = payload["manifest"].get("format_version")
    if fmt != FORMAT_VERSION:
        raise BackupFormatError(
            f"{path}: format_version={fmt}, this build expects {FORMAT_VERSION}"
        )
    return payload


# ── Errors ──────────────────────────────────────────────────────────────────


class BackupFormatError(ValueError):
    """Raised on malformed backup files."""


# ── Helpers ─────────────────────────────────────────────────────────────────


def _dump_table(db: Database, table_name: str) -> list[dict[str, Any]]:
    """Dump every row of ``table_name`` as JSON-serializable dicts.

    We rely on SQLAlchemy reflection rather than the SQLModel registry so
    backup also captures rows the running process doesn't have a Python
    class for (forward-compat with later schema additions). Uses the engine
    directly (rather than ``session.exec``) so Core ``select(Table)`` returns
    ordinary Row objects without SQLModel's entity-mapping interference."""
    from sqlalchemy import MetaData, Table, select as core_select

    md = MetaData()
    tbl = Table(table_name, md, autoload_with=db.engine)
    cols = [c.name for c in tbl.columns]
    out: list[dict[str, Any]] = []
    with db.engine.connect() as conn:
        result = conn.execute(core_select(tbl))
        for row in result.mappings():
            # row.mappings() yields dict-like RowMapping objects keyed by column.
            record: dict[str, Any] = {}
            for name in cols:
                record[name] = _serialize_value(row[name])
            out.append(record)
    return out


def _serialize_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _json_fallback(v: Any) -> Any:
    """For dataclasses + datetimes that survive into the json.dump call."""
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "__dict__"):
        return v.__dict__
    return str(v)


def _read_schema_head(db: Database) -> str | None:
    from sqlalchemy import text

    try:
        with db.session() as s:
            row = s.exec(text("SELECT version_num FROM alembic_version")).first()
        return row[0] if row else None
    except Exception:
        return None


def _agent_core_version() -> str:
    try:
        from importlib.metadata import version

        return version("agent-core")
    except Exception:
        return "0.0.1"


__all__ = [
    "FORMAT_VERSION",
    "BackupFormatError",
    "BackupManifest",
    "create_backup",
    "read_backup",
    "write_backup",
]
