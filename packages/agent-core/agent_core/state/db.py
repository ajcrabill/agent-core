"""Dual-backend Database for agent-core.

Default per product:
  - dcos-agent: SQLite (path: ~/.local/state/<instance>/agent.db)
  - ikb-agent:  PostgreSQL (DSN: dbname=<instance> host=/tmp)

The Database class is a thin wrapper over SQLAlchemy's engine + SQLModel's
Session, with backend-specific defaults:

  SQLite per-connection PRAGMAs:
    - foreign_keys = ON           (FK constraints actually enforced)
    - journal_mode = WAL           (concurrent reads with single writer)
    - synchronous = NORMAL         (durability/perf tradeoff appropriate for WAL)
    - busy_timeout = 5000          (5s wait under contention; avoids SQLITE_BUSY)

  PostgreSQL: defaults to local Unix socket (host=/tmp), psycopg3 driver.

Sprint 1.3 will add Alembic migrations on top of this; for now `create_all()`
bootstraps the schema on a fresh install.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)


class Backend(StrEnum):
    sqlite = "sqlite"
    postgres = "postgres"


# ── Defaults ─────────────────────────────────────────────────────────────────


def default_sqlite_path(instance_name: str = "agent") -> Path:
    """Where SQLite stores the agent's database by default.

    Follows the XDG state-directory convention:
      ~/.local/state/<instance_name>/agent.db

    `instance_name` should be set by the install wizard to whatever the user
    named their agent — but we never bake that into the package itself.
    """
    return Path.home() / ".local" / "state" / instance_name / "agent.db"


def default_postgres_dsn(database: str = "agent_core") -> str:
    """libpq-style DSN for the local Postgres socket convention."""
    return f"dbname={database} host=/tmp"


# ── SQLite per-connection setup ──────────────────────────────────────────────


def _on_sqlite_connect(dbapi_connection: Any, connection_record: Any) -> None:
    """SQLAlchemy `connect` event handler — runs once per new DB-API connection.

    Sets the per-connection PRAGMAs that make SQLite behave reasonably for an
    agent's mixed read/write workload.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
    finally:
        cursor.close()


# ── DSN translation ──────────────────────────────────────────────────────────


def libpq_dsn_to_sqlalchemy_url(dsn: str) -> str:
    """Convert a libpq DSN string to a SQLAlchemy-compatible URL.

    Example:
        ``dbname=agent_core host=/tmp`` →
        ``postgresql+psycopg:///?dbname=agent_core&host=%2Ftmp``

    SQLAlchemy supports passing libpq parameters via the URL query string,
    which keeps Unix-socket paths and other quirks portable.
    """
    params: dict[str, str] = {}
    for token in dsn.strip().split():
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        params[k.strip()] = v.strip()
    if not params:
        raise ValueError(f"invalid libpq DSN: {dsn!r}")
    query = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    return f"postgresql+psycopg:///?{query}"


# ── Database ─────────────────────────────────────────────────────────────────


class Database:
    """Dual-backend database wrapper for agent-core.

    Construct via the :meth:`sqlite`, :meth:`sqlite_memory`, or :meth:`postgres`
    classmethods; or pass a SQLAlchemy URL directly to ``__init__``.

    Use :meth:`session` as a context manager for session scope; rolls back on
    exception, no automatic commit.
    """

    def __init__(self, url: str, *, echo: bool = False, **engine_kwargs: Any) -> None:
        self.url = url
        self.backend = self._detect_backend(url)
        self._engine: Engine = self._build_engine(url, echo=echo, **engine_kwargs)

    # ── Construction helpers ────────────────────────────────────────────────

    @classmethod
    def sqlite(
        cls,
        path: str | Path | None = None,
        *,
        instance_name: str = "agent",
        echo: bool = False,
    ) -> Database:
        """Build a SQLite-backed Database. If ``path`` is None, uses
        :func:`default_sqlite_path` with the given ``instance_name``."""
        if path is None:
            path = default_sqlite_path(instance_name)
        path = Path(path).expanduser().resolve()
        return cls(f"sqlite:///{path}", echo=echo)

    @classmethod
    def sqlite_memory(cls, *, echo: bool = False) -> Database:
        """Build an in-memory SQLite Database. Useful for tests."""
        return cls("sqlite:///:memory:", echo=echo)

    @classmethod
    def postgres(cls, dsn: str | None = None, *, echo: bool = False) -> Database:
        """Build a PostgreSQL-backed Database. ``dsn`` accepts libpq form
        (``dbname=… host=…``); if None, uses :func:`default_postgres_dsn`."""
        if dsn is None:
            dsn = default_postgres_dsn()
        url = libpq_dsn_to_sqlalchemy_url(dsn)
        return cls(url, echo=echo)

    # ── Engine + connection lifecycle ───────────────────────────────────────

    @staticmethod
    def _detect_backend(url: str) -> Backend:
        if url.startswith("sqlite"):
            return Backend.sqlite
        if url.startswith(("postgresql", "postgres")):
            return Backend.postgres
        raise ValueError(f"unsupported backend in URL: {url!r}")

    def _build_engine(self, url: str, *, echo: bool, **kwargs: Any) -> Engine:
        eng = create_engine(url, echo=echo, **kwargs)
        if self.backend == Backend.sqlite:
            event.listen(eng, "connect", _on_sqlite_connect)
        return eng

    @property
    def engine(self) -> Engine:
        return self._engine

    def close(self) -> None:
        """Dispose the engine and release pool connections."""
        self._engine.dispose()

    def sqlite_path(self) -> Path | None:
        """Return the file path for SQLite URLs; ``None`` for in-memory or for
        non-SQLite backends."""
        if self.backend != Backend.sqlite:
            return None
        if ":memory:" in self.url:
            return None
        if self.url.startswith("sqlite:///"):
            return Path(self.url.removeprefix("sqlite:///"))
        return None

    # ── Schema lifecycle ────────────────────────────────────────────────────

    def create_all(self) -> None:
        """Create all tables registered on ``SQLModel.metadata``.

        Use only on fresh installs. Sprint 1.3 introduces Alembic; from that
        point forward, migrations are the supported path for schema changes.
        """
        if self.backend == Backend.sqlite:
            db_path = self.sqlite_path()
            if db_path is not None:
                db_path.parent.mkdir(parents=True, exist_ok=True)
        SQLModel.metadata.create_all(self._engine)
        logger.info(
            "schema bootstrapped: %d tables on %s",
            len(SQLModel.metadata.tables),
            self.backend.value,
        )

    # ── Session ─────────────────────────────────────────────────────────────

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a SQLModel Session as a context manager.

        Rolls back on uncaught exception. Caller commits explicitly:
            with db.session() as s:
                s.add(record)
                s.commit()
        """
        s = Session(self._engine)
        try:
            yield s
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ── Health ──────────────────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        """Round-trip a trivial query. Used by ``agent-core doctor``."""
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.warning("db health check failed: %s", e)
            return False


__all__ = [
    "Backend",
    "Database",
    "default_postgres_dsn",
    "default_sqlite_path",
    "libpq_dsn_to_sqlalchemy_url",
]
