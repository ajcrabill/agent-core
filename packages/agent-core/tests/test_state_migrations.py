"""Alembic migration tests for agent_core.state.

Verifies:
  - There's exactly one head revision (no accidental branching)
  - upgrade('head') brings a fresh DB to the head revision
  - upgrade is idempotent (second invocation = no-op)
  - current_revision() returns the head after upgrade
  - The set of tables after upgrade matches the SQLModel metadata exactly
    (besides alembic_version)
  - File DBs auto-create their parent directory
"""

from __future__ import annotations

from pathlib import Path

from agent_core.state import Database
from sqlalchemy import inspect
from sqlmodel import SQLModel


def test_exactly_one_head_revision() -> None:
    """A healthy migration tree has a single head; multiple heads indicate
    accidental branching (someone created two migrations without merging)."""
    db = Database.sqlite_memory()
    heads = db.heads()
    assert len(heads) == 1, f"expected single head, got {heads}"


def test_current_revision_none_before_upgrade() -> None:
    db = Database.sqlite_memory()
    assert db.current_revision() is None


def test_upgrade_brings_db_to_head() -> None:
    db = Database.sqlite_memory()
    db.upgrade()
    head = db.heads()[0]
    assert db.current_revision() == head


def test_upgrade_is_idempotent() -> None:
    db = Database.sqlite_memory()
    db.upgrade()
    rev_after_first = db.current_revision()
    db.upgrade()  # should be a no-op
    assert db.current_revision() == rev_after_first


def test_upgrade_creates_all_expected_tables() -> None:
    """Upgrading a fresh DB should produce the same set of tables as
    SQLModel.metadata.create_all() — modulo the alembic_version table."""
    db_alembic = Database.sqlite_memory()
    db_alembic.upgrade()
    eng_alembic_tables = set(inspect(db_alembic.engine).get_table_names())

    db_create = Database.sqlite_memory()
    db_create.create_all()
    eng_create_tables = set(inspect(db_create.engine).get_table_names())

    # Alembic adds its bookkeeping table; otherwise the table sets must match.
    assert eng_alembic_tables - {"alembic_version"} == eng_create_tables
    # And both should have all the SQLModel-declared tables
    declared = set(SQLModel.metadata.tables.keys())
    assert eng_create_tables == declared


def test_upgrade_creates_parent_dir_for_sqlite_file(tmp_path: Path) -> None:
    nested = tmp_path / "fresh" / "install" / "agent.db"
    db = Database.sqlite(nested)
    assert not nested.parent.exists()
    db.upgrade()
    assert nested.exists()
    assert db.current_revision() is not None


def test_session_works_after_upgrade(tmp_path: Path) -> None:
    """End-to-end: upgrade() + use the schema."""
    from agent_core.state import Identity

    db = Database.sqlite(tmp_path / "x.db")
    db.upgrade()
    with db.session() as s:
        s.add(Identity(instance_name="post-upgrade-test"))
        s.commit()
    with db.session() as s:
        assert s.get(Identity, "self").instance_name == "post-upgrade-test"
