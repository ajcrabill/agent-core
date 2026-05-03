"""Database class smoke tests.

Verifies:
  - Backend detection from URL
  - SQLite per-connection PRAGMAs are applied (foreign_keys, WAL, busy_timeout)
  - SQLite file path defaults + parent dir auto-creation
  - libpq DSN → SQLAlchemy URL translation
  - In-memory create_all + session roundtrip
  - Session context manager rolls back on exception
  - is_healthy returns True for working backend
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_core.state import (
    Backend,
    Database,
    Identity,
    Obligation,
    default_postgres_dsn,
    default_sqlite_path,
    libpq_dsn_to_sqlalchemy_url,
)
from sqlalchemy import text


def test_backend_detection() -> None:
    assert Database("sqlite:///:memory:").backend == Backend.sqlite
    assert Database("sqlite:///tmp/x.db").backend == Backend.sqlite
    # postgres URL form (no actual connection needed for backend detection)
    db = Database("postgresql+psycopg:///?dbname=test&host=%2Ftmp")
    assert db.backend == Backend.postgres


def test_unsupported_backend_raises() -> None:
    with pytest.raises(ValueError, match="unsupported backend"):
        Database("mysql://user@host/db")


def test_default_sqlite_path_under_xdg_state() -> None:
    p = default_sqlite_path("dCoS")
    assert p == Path.home() / ".local" / "state" / "dCoS" / "agent.db"


def test_default_postgres_dsn() -> None:
    assert default_postgres_dsn() == "dbname=agent_core host=/tmp"
    assert default_postgres_dsn("ikb") == "dbname=ikb host=/tmp"


def test_libpq_dsn_to_url() -> None:
    url = libpq_dsn_to_sqlalchemy_url("dbname=agent_core host=/tmp")
    assert url.startswith("postgresql+psycopg:///?")
    assert "dbname=agent_core" in url
    # /tmp gets URL-quoted to %2Ftmp (since '/' is unsafe in query strings)
    assert "host=%2Ftmp" in url


def test_libpq_dsn_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid libpq DSN"):
        libpq_dsn_to_sqlalchemy_url("not_a_dsn")
    with pytest.raises(ValueError, match="invalid libpq DSN"):
        libpq_dsn_to_sqlalchemy_url("")


def test_libpq_dsn_url_quoting() -> None:
    """Special characters in DSN values must be URL-quoted."""
    url = libpq_dsn_to_sqlalchemy_url("dbname=ag&core host=/var/run/postgres")
    assert "dbname=ag%26core" in url
    assert "host=%2Fvar%2Frun%2Fpostgres" in url


def test_sqlite_memory_create_all_and_roundtrip() -> None:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(Identity(instance_name="TestAgent"))
        s.commit()
    with db.session() as s:
        assert s.get(Identity, "self").instance_name == "TestAgent"


def test_sqlite_file_creates_parent_dir(tmp_path: Path) -> None:
    """create_all should mkdir the parent dir for sqlite file paths so a
    fresh install Just Works."""
    nested = tmp_path / "deep" / "nested" / "agent.db"
    db = Database.sqlite(nested)
    assert not nested.parent.exists()
    db.create_all()
    assert nested.parent.exists()
    assert nested.exists()


def test_sqlite_pragmas_applied(tmp_path: Path) -> None:
    """The per-connection event handler should set foreign_keys, journal_mode,
    busy_timeout."""
    db = Database.sqlite(tmp_path / "p.db")
    db.create_all()
    with db.engine.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
        jm = conn.execute(text("PRAGMA journal_mode")).scalar()
        bt = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert fk == 1, "foreign_keys should be ON"
    assert jm == "wal", f"journal_mode should be WAL, got {jm!r}"
    assert bt == 5000, f"busy_timeout should be 5000ms, got {bt}"


def test_session_rollback_on_exception() -> None:
    db = Database.sqlite_memory()
    db.create_all()
    # First insert succeeds
    with db.session() as s:
        s.add(Obligation(title="committed"))
        s.commit()
    # Now raise inside a session — the session's pending changes should not
    # land in the db.
    with pytest.raises(RuntimeError), db.session() as s:
        s.add(Obligation(title="not committed"))
        # No commit; raise before
        raise RuntimeError("simulated failure")
    # Verify only the first one is there
    from sqlmodel import select

    with db.session() as s:
        rows = s.exec(select(Obligation)).all()
    assert len(rows) == 1
    assert rows[0].title == "committed"


def test_is_healthy_in_memory_sqlite() -> None:
    db = Database.sqlite_memory()
    assert db.is_healthy() is True


def test_is_healthy_returns_false_for_unreachable_postgres() -> None:
    """Use a non-existent DSN; is_healthy should swallow the connection error
    and return False (not raise) — agent-core doctor needs a clean signal."""
    db = Database("postgresql+psycopg:///?dbname=nope_does_not_exist&host=%2Ftmp")
    assert db.is_healthy() is False


def test_close_disposes_engine() -> None:
    db = Database.sqlite_memory()
    db.create_all()
    db.close()
    # After close, a new connection can still be obtained from the pool
    # (SQLAlchemy re-establishes), but the dispose() call should have
    # released the prior pool. Smoke-test that close() doesn't raise.


def test_sqlite_path_returns_file_path_for_file_url(tmp_path: Path) -> None:
    p = tmp_path / "x.db"
    db = Database.sqlite(p)
    assert db.sqlite_path() == p


def test_sqlite_path_returns_none_for_memory() -> None:
    db = Database.sqlite_memory()
    assert db.sqlite_path() is None


def test_sqlite_path_returns_none_for_postgres() -> None:
    db = Database("postgresql+psycopg:///?dbname=test&host=%2Ftmp")
    assert db.sqlite_path() is None
