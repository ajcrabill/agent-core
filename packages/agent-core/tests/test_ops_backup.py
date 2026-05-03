"""Tests for agent_core.ops.backup + restore — round-trip + safety gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agent_core.ops import (
    BackupFormatError,
    RestoreNotConfirmedError,
    RestoreSchemaMismatchError,
    create_backup,
    read_backup,
    restore_backup,
    write_backup,
)
from agent_core.ops.backup import FORMAT_VERSION
from agent_core.settings import AgentSettings, SettingsManager
from agent_core.state import Database
from agent_core.state.models import Obligation, ObligationSource, ObligationStatus


# ── Fixtures ────────────────────────────────────────────────────────────────


def _db_with_data(tmp_path: Path) -> Database:
    """Build a file-backed db with a couple of obligations."""
    db_path = tmp_path / "test.db"
    db = Database.sqlite(db_path)
    db.create_all()
    with db.session() as s:
        s.add(
            Obligation(
                title="Test obligation",
                source=ObligationSource.manual,
                status=ObligationStatus.in_progress,
            )
        )
        s.add(
            Obligation(
                title="Another one",
                source=ObligationSource.manual,
                status=ObligationStatus.waiting,
            )
        )
        s.commit()
    return db


# ── Backup round-trip ──────────────────────────────────────────────────────


def test_create_backup_includes_manifest_and_tables(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    payload = create_backup(db)
    assert payload["manifest"]["format_version"] == FORMAT_VERSION
    assert "tables" in payload
    assert "obligation" in payload["tables"]
    assert len(payload["tables"]["obligation"]) == 2


def test_create_backup_skips_alembic_table(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    payload = create_backup(db)
    # alembic_version isn't a user table; it's tracked in manifest.schema_head
    # only if the alembic version table exists. The DB created by create_all()
    # may not have it — either way, it should NOT be in the tables payload.
    assert "alembic_version" not in payload["tables"]


def test_create_backup_records_table_counts(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    payload = create_backup(db)
    counts = payload["manifest"]["tables"]
    assert counts.get("obligation") == 2


def test_create_backup_includes_settings_when_passed(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    s = AgentSettings(autonomy={"default_policy": "cautious"})  # type: ignore[arg-type]
    payload = create_backup(db, settings=s)
    assert payload["manifest"]["includes_settings"] is True
    assert payload["settings"]["autonomy"]["default_policy"] == "cautious"


def test_create_backup_with_settings_path_embeds_yaml(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    yml = tmp_path / "agent.yml"
    yml.write_text(yaml.safe_dump({"autonomy": {"default_policy": "aggressive"}}))
    payload = create_backup(db, settings_path=yml)
    assert payload["manifest"]["includes_settings"] is True
    assert "settings_yaml" in payload
    assert "aggressive" in payload["settings_yaml"]


def test_create_backup_excludes_identity_by_default(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    payload = create_backup(db)
    assert payload["manifest"]["includes_identity"] is False
    assert "identity" not in payload


def test_create_backup_includes_identity_when_explicitly_opted_in(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    payload = create_backup(
        db, include_identity=True, identity_public_key="abcd1234"
    )
    assert payload["manifest"]["includes_identity"] is True
    assert payload["identity"]["public_key"] == "abcd1234"


def test_create_backup_identity_requires_public_key(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    with pytest.raises(ValueError, match="identity_public_key"):
        create_backup(db, include_identity=True)


# ── write/read ──────────────────────────────────────────────────────────────


def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    payload = create_backup(db)
    out = tmp_path / "backup.json"
    write_backup(payload, out)
    assert out.exists()
    loaded = read_backup(out)
    assert loaded["manifest"]["format_version"] == FORMAT_VERSION
    assert loaded["tables"]["obligation"] == payload["tables"]["obligation"]


def test_read_backup_rejects_missing_manifest(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tables": {}}))
    with pytest.raises(BackupFormatError):
        read_backup(bad)


def test_read_backup_rejects_wrong_format_version(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"manifest": {"format_version": 999}, "tables": {}})
    )
    with pytest.raises(BackupFormatError, match="format_version"):
        read_backup(bad)


def test_write_backup_is_atomic(tmp_path: Path) -> None:
    """If write_backup is interrupted mid-write (sim. by an existing
    same-named file on disk being overwritten), the original isn't corrupted."""
    db = _db_with_data(tmp_path)
    out = tmp_path / "backup.json"
    out.write_text('{"old": "stuff"}')
    payload = create_backup(db)
    write_backup(payload, out)
    # Loaded text should be the new payload, not concatenated/half-written.
    loaded = json.loads(out.read_text())
    assert "manifest" in loaded
    assert "old" not in loaded


# ── Restore safety ─────────────────────────────────────────────────────────


def test_restore_requires_explicit_confirm(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    payload = create_backup(db)
    with pytest.raises(RestoreNotConfirmedError):
        restore_backup(db, payload)  # confirm=False (default)


def test_restore_round_trip_replaces_data(tmp_path: Path) -> None:
    src = _db_with_data(tmp_path)
    payload = create_backup(src)

    # Build a fresh empty target db
    target_path = tmp_path / "target.db"
    target = Database.sqlite(target_path)
    target.create_all()

    # Add a stray row to the target so we can verify restore wipes it.
    with target.session() as s:
        s.add(
            Obligation(
                title="Stray that should be wiped",
                source=ObligationSource.manual,
            )
        )
        s.commit()

    report = restore_backup(target, payload, confirm=True, skip_schema_check=True)
    assert report.rows_inserted.get("obligation") == 2

    # Verify only the restored rows survived.
    from sqlmodel import select

    with target.session() as s:
        rows = list(s.exec(select(Obligation)).all())
    titles = {r.title for r in rows}
    assert titles == {"Test obligation", "Another one"}


def test_restore_schema_mismatch_raises(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    payload = create_backup(db)
    # Forge a schema-head mismatch
    payload["manifest"]["schema_head"] = "fake-head-value"

    # Inject an alembic_version row so the comparison actually happens
    from sqlalchemy import text

    target_path = tmp_path / "target.db"
    target = Database.sqlite(target_path)
    target.create_all()
    with target.session() as s:
        s.exec(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        s.exec(text("INSERT INTO alembic_version VALUES ('actual-current-head')"))
        s.commit()

    with pytest.raises(RestoreSchemaMismatchError):
        restore_backup(target, payload, confirm=True)


def test_restore_skip_schema_check_bypasses_mismatch(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    payload = create_backup(db)
    payload["manifest"]["schema_head"] = "fake"

    target_path = tmp_path / "target.db"
    target = Database.sqlite(target_path)
    target.create_all()
    # No alembic_version table → no comparison attempted, so no mismatch
    # even without skip_schema_check. Add it to make the test meaningful.
    from sqlalchemy import text

    with target.session() as s:
        s.exec(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        s.exec(text("INSERT INTO alembic_version VALUES ('different-head')"))
        s.commit()

    # With skip flag, restore proceeds despite mismatch
    report = restore_backup(target, payload, confirm=True, skip_schema_check=True)
    assert report.rows_inserted.get("obligation") == 2


def test_restore_writes_settings_when_path_provided(tmp_path: Path) -> None:
    db = _db_with_data(tmp_path)
    yml = tmp_path / "src-agent.yml"
    yml.write_text(yaml.safe_dump({"autonomy": {"default_policy": "cautious"}}))
    payload = create_backup(db, settings_path=yml)

    # Restore into a fresh target with a different settings path
    target_path = tmp_path / "target.db"
    target = Database.sqlite(target_path)
    target.create_all()
    target_yml = tmp_path / "target-agent.yml"

    report = restore_backup(
        target, payload, confirm=True, settings_path=target_yml, skip_schema_check=True
    )
    assert report.settings_written is True
    assert "cautious" in target_yml.read_text()


def test_restore_skips_unknown_tables(tmp_path: Path) -> None:
    """Backup carrying a future table not in the current schema → skipped, not crash."""
    db = _db_with_data(tmp_path)
    payload = create_backup(db)
    payload["tables"]["future_table_we_dont_know"] = [{"id": 1}]

    target_path = tmp_path / "target.db"
    target = Database.sqlite(target_path)
    target.create_all()

    report = restore_backup(target, payload, confirm=True, skip_schema_check=True)
    assert "future_table_we_dont_know" in report.skipped_tables
