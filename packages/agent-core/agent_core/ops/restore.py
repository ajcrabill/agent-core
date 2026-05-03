"""Restore agent state from a backup file.

This is destructive on the target — it clears existing rows in every table
the backup carries, then bulk-inserts. The CLI surfaces a confirmation
prompt; programmatic callers must pass ``confirm=True``.

Schema compatibility:
    - Restores only succeed when the backup's ``schema_head`` matches the
      current database. Mismatch returns a ``RestoreSchemaMismatchError``
      with both versions, so the user knows whether to upgrade their
      install or downgrade the backup tooling.
    - We do NOT auto-run migrations during restore. Restoring into a
      partially-migrated db would leave the user with rows in tables their
      schema doesn't fully understand. Better to fail loudly.

Settings + identity behavior:
    - If the backup includes ``settings_yaml`` and ``settings_path`` is
      passed, the file is overwritten (same atomic-write pattern as
      SettingsManager).
    - Identity is never auto-restored — the public bundle is in the file
      but applying it requires the user's SecretStore choices, which we
      don't second-guess.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, inspect

from agent_core.state.db import Database

logger = logging.getLogger(__name__)


# ── Errors ──────────────────────────────────────────────────────────────────


class RestoreError(RuntimeError):
    """Base class for restore failures."""


class RestoreSchemaMismatchError(RestoreError):
    """Backup schema_head doesn't match the current database."""


class RestoreNotConfirmedError(RestoreError):
    """Caller didn't pass ``confirm=True`` (CLI confirmation gate)."""


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass
class RestoreReport:
    """Summary of what restore did."""

    rows_inserted: dict[str, int] = field(default_factory=dict)
    rows_cleared: dict[str, int] = field(default_factory=dict)
    settings_written: bool = False
    skipped_tables: list[str] = field(default_factory=list)


# ── Restore ─────────────────────────────────────────────────────────────────


def restore_backup(
    db: Database,
    payload: dict[str, Any],
    *,
    confirm: bool = False,
    settings_path: Path | None = None,
    skip_schema_check: bool = False,
) -> RestoreReport:
    """Replace current state with the contents of ``payload``.

    Args:
        db: Target database. Will be modified destructively.
        payload: Result of ``read_backup()``.
        confirm: Must be True. Defense against accidental restore.
        settings_path: If set and the backup carries ``settings_yaml``,
            overwrite this file with the backup's settings.
        skip_schema_check: Override the schema-head check. Only use this
            when migrating between known-compatible schema versions.

    Returns:
        ``RestoreReport`` summarizing inserts/clears.
    """
    if not confirm:
        raise RestoreNotConfirmedError(
            "restore_backup requires confirm=True (this overwrites every table)"
        )

    manifest = payload.get("manifest", {})
    backup_head = manifest.get("schema_head")

    if not skip_schema_check:
        current_head = _read_schema_head(db)
        if backup_head and current_head and backup_head != current_head:
            raise RestoreSchemaMismatchError(
                f"backup at schema {backup_head!r}, current db at {current_head!r}; "
                f"upgrade one or pass skip_schema_check=True if you know they're compatible"
            )

    report = RestoreReport()
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    tables_payload: dict[str, list[dict[str, Any]]] = payload.get("tables", {})

    md = MetaData()

    # Use the engine directly (not SQLModel session.exec) so Core Table ops
    # don't go through entity-mapping. Single transaction so partial failure
    # rolls back cleanly.
    with db.engine.begin() as conn:
        # Clear everything first, then insert. agent-core's schema is flat
        # enough that we don't need to topologically sort by FK.
        for table_name in tables_payload:
            if table_name not in existing_tables:
                report.skipped_tables.append(table_name)
                continue
            tbl = Table(table_name, md, autoload_with=db.engine)
            result = conn.execute(tbl.delete())
            report.rows_cleared[table_name] = result.rowcount or 0

        for table_name, rows in tables_payload.items():
            if table_name not in existing_tables:
                continue
            if not rows:
                report.rows_inserted[table_name] = 0
                continue
            tbl = Table(table_name, md, autoload_with=db.engine)
            # Coerce ISO-string datetimes (and date) back to Python objects;
            # SQLite's strict DateTime type refuses raw strings.
            coerced = [_coerce_row(row, tbl) for row in rows]
            conn.execute(tbl.insert(), coerced)
            report.rows_inserted[table_name] = len(rows)

    if settings_path is not None and "settings_yaml" in payload:
        _atomic_write_text(settings_path, payload["settings_yaml"])
        report.settings_written = True

    logger.info(
        "restore complete: cleared=%d inserted=%d settings_written=%s",
        sum(report.rows_cleared.values()),
        sum(report.rows_inserted.values()),
        report.settings_written,
    )
    return report


# ── Helpers ─────────────────────────────────────────────────────────────────


def _read_schema_head(db: Database) -> str | None:
    from sqlalchemy import text

    try:
        with db.session() as s:
            row = s.exec(text("SELECT version_num FROM alembic_version")).first()
        return row[0] if row else None
    except Exception:
        return None


def _coerce_row(row: dict[str, Any], tbl: Table) -> dict[str, Any]:
    """Convert ISO-string datetimes/dates back to Python objects so the
    DB driver accepts them. Other columns pass through unchanged."""
    from datetime import date, datetime

    from sqlalchemy.types import Date, DateTime

    out: dict[str, Any] = {}
    for col in tbl.columns:
        name = col.name
        if name not in row:
            continue
        val = row[name]
        if val is None:
            out[name] = None
            continue
        col_type = col.type
        if isinstance(col_type, DateTime) and isinstance(val, str):
            try:
                out[name] = datetime.fromisoformat(val)
            except ValueError as e:
                raise ValueError(
                    f"row column {name!r} (DateTime) had invalid value {val!r}: {e}"
                ) from e
        elif isinstance(col_type, Date) and isinstance(val, str):
            out[name] = date.fromisoformat(val)
        else:
            out[name] = val
    return out


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


__all__ = [
    "RestoreError",
    "RestoreNotConfirmedError",
    "RestoreReport",
    "RestoreSchemaMismatchError",
    "restore_backup",
]
