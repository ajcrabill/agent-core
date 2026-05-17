"""Health checks for an installed agent.

The user runs ``agent doctor`` after install or whenever something feels
off. Each check is a small object with a name + a ``run(context)`` method
that returns a ``CheckResult`` (ok / warn / fail / skipped, plus a human
message and optional structured details).

Adding a check (one place to look):

    @register
    class MyCheck:
        name = "my-thing"
        def run(self, ctx: DoctorContext) -> CheckResult:
            if some_problem:
                return CheckResult(name=self.name, status=CheckStatus.fail,
                                   message="...", details={...})
            return CheckResult(name=self.name, status=CheckStatus.ok,
                               message="...")

Design rules:
    - A check that doesn't apply to this install returns ``skipped`` —
      never ``fail``. (E.g., "ollama reachable" when embedding_provider='stub'.)
    - Network checks have a short timeout (default 3s). Doctor must not
      block the user for minutes.
    - Checks read from settings — they don't take their own constructor
      args. The whole point is to verify the *configured* state.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Result types ────────────────────────────────────────────────────────────


class CheckStatus(StrEnum):
    """Outcome of a single check.

    ok       — feature works as expected
    warn     — feature works but something to know (e.g. degraded)
    fail     — feature is broken; user action needed
    skipped  — feature isn't configured on this install; not applicable
    """

    ok = "ok"
    warn = "warn"
    fail = "fail"
    skipped = "skipped"


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome."""

    name: str
    status: CheckStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorContext:
    """Bag of stuff every check might want.

    ``settings`` is required; ``db`` is optional so checks that don't need
    the database don't force the doctor to construct one (useful for the
    pre-install case where the db doesn't exist yet)."""

    settings: object  # AgentSettings
    db: Any = None  # Database or None


@dataclass
class DoctorReport:
    """Aggregate result of ``Doctor.run()``."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if every non-skipped check passed (warns count as ok)."""
        return not any(r.status == CheckStatus.fail for r in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(r.status == CheckStatus.warn for r in self.results)

    def by_status(self) -> dict[CheckStatus, int]:
        out: dict[CheckStatus, int] = dict.fromkeys(CheckStatus, 0)
        for r in self.results:
            out[r.status] += 1
        return out


# ── Check Protocol ──────────────────────────────────────────────────────────


@runtime_checkable
class HealthCheck(Protocol):
    """A single thing the doctor can verify."""

    name: str

    def run(self, ctx: DoctorContext) -> CheckResult: ...


# ── Built-in checks ────────────────────────────────────────────────────────


class SettingsValidCheck:
    """The settings file resolves cleanly + reports source of every value."""

    name = "settings"

    def run(self, ctx: DoctorContext) -> CheckResult:
        # If settings is a SettingsManager, ask it to reload (catches drift
        # since startup). If it's a bare AgentSettings, the fact we have it
        # means it already validated.
        s = ctx.settings
        try:
            if hasattr(s, "reload"):
                s.reload()
            return CheckResult(name=self.name, status=CheckStatus.ok, message="loaded cleanly")
        except Exception as e:
            return CheckResult(
                name=self.name,
                status=CheckStatus.fail,
                message=f"failed to reload: {e}",
            )


class StorageReachableCheck:
    """Database is up, schema present (we can query a known table)."""

    name = "storage"

    def run(self, ctx: DoctorContext) -> CheckResult:
        if ctx.db is None:
            return CheckResult(
                name=self.name,
                status=CheckStatus.skipped,
                message="no database in context",
            )
        try:
            from sqlmodel import select

            from agent_core.state.models import Obligation

            with ctx.db.session() as s:
                # We don't care about the count; just that the query runs.
                _ = s.exec(select(Obligation).limit(1)).first()
            backend = _settings_attr(ctx.settings, "storage", "backend", default="?")
            return CheckResult(
                name=self.name,
                status=CheckStatus.ok,
                message=f"reachable ({backend})",
                details={"backend": backend},
            )
        except Exception as e:
            return CheckResult(
                name=self.name,
                status=CheckStatus.fail,
                message=f"query failed: {e}",
            )


class MigrationsAtHeadCheck:
    """Alembic version_num matches the latest migration in the package."""

    name = "migrations"

    def run(self, ctx: DoctorContext) -> CheckResult:
        if ctx.db is None:
            return CheckResult(
                name=self.name, status=CheckStatus.skipped, message="no database in context"
            )
        try:
            from sqlalchemy import text

            with ctx.db.session() as s:
                row = s.exec(text("SELECT version_num FROM alembic_version")).first()
            current = row[0] if row else None
            head = _alembic_head_revision()
            if current == head:
                return CheckResult(
                    name=self.name,
                    status=CheckStatus.ok,
                    message=f"at head ({current})",
                    details={"current": current, "head": head},
                )
            return CheckResult(
                name=self.name,
                status=CheckStatus.warn,
                message=(f"current={current}, head={head} — run `alembic upgrade head`"),
                details={"current": current, "head": head},
            )
        except Exception as e:
            # Most likely: alembic_version table missing → user hasn't run
            # migrations at all. Surface as warn (not fail) so doctor still
            # exits 0 on a brand-new install that needs `alembic upgrade head`.
            return CheckResult(
                name=self.name,
                status=CheckStatus.warn,
                message=f"could not read alembic_version: {e}",
            )


class VaultPathCheck:
    """If a vault path is configured, it exists and is writable."""

    name = "vault"

    def run(self, ctx: DoctorContext) -> CheckResult:
        path_str = _settings_attr(ctx.settings, "storage", "vault_path", default=None)
        if not path_str:
            return CheckResult(
                name=self.name, status=CheckStatus.skipped, message="no vault configured"
            )
        path = Path(path_str)
        if not path.exists():
            return CheckResult(
                name=self.name,
                status=CheckStatus.fail,
                message=f"vault path does not exist: {path}",
            )
        if not path.is_dir():
            return CheckResult(
                name=self.name,
                status=CheckStatus.fail,
                message=f"vault path is not a directory: {path}",
            )
        # Quick writability probe: try to stat (cheap) and check os.access.
        import os

        if not os.access(path, os.W_OK):
            return CheckResult(
                name=self.name,
                status=CheckStatus.warn,
                message=f"vault not writable: {path}",
            )
        return CheckResult(name=self.name, status=CheckStatus.ok, message=f"ok at {path}")


class OllamaReachableCheck:
    """If embedding_provider='ollama', the configured base_url answers AND
    the configured embedding_model is actually pulled.

    The "model is pulled" sub-check matters because Ollama returns 404 at
    embedding-call time when a model isn't local — which surfaces as a
    confusing crash inside `<product> remember` rather than a clear
    diagnostic. We catch it at doctor time instead.
    """

    name = "ollama"
    timeout = 3.0

    def run(self, ctx: DoctorContext) -> CheckResult:
        provider = _settings_attr(ctx.settings, "openbrain", "embedding_provider", default=None)
        if provider != "ollama":
            return CheckResult(
                name=self.name,
                status=CheckStatus.skipped,
                message=f"embedding_provider={provider!r} (not ollama)",
            )
        base = _settings_attr(
            ctx.settings, "openbrain", "ollama_base_url", default="http://localhost:11434"
        )
        model = _settings_attr(
            ctx.settings, "openbrain", "embedding_model", default="nomic-embed-text"
        )
        url = f"{base.rstrip('/')}/api/tags"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                if not 200 <= resp.status < 300:
                    return CheckResult(
                        name=self.name,
                        status=CheckStatus.warn,
                        message=f"unexpected HTTP {resp.status} from {url}",
                    )
                # Parse the model list and verify ours is present. Ollama
                # tolerates "<name>" matching "<name>:latest" for tagless
                # references; we mirror that tolerance.
                try:
                    body = json.loads(resp.read().decode())
                    available = {m.get("name", "") for m in body.get("models", [])}
                except Exception:
                    return CheckResult(
                        name=self.name,
                        status=CheckStatus.ok,
                        message=f"reachable at {base} (could not parse model list)",
                    )
                wanted = model if ":" in model else f"{model}:latest"
                if model in available or wanted in available:
                    return CheckResult(
                        name=self.name,
                        status=CheckStatus.ok,
                        message=f"reachable at {base}; embedding model {model!r} pulled",
                    )
                return CheckResult(
                    name=self.name,
                    status=CheckStatus.warn,
                    message=(
                        f"reachable at {base}, but embedding model {model!r} "
                        f"is not pulled — run `ollama pull {model}` "
                        "(otherwise OpenBrain capture will 404 at embedding time)"
                    ),
                )
        except (urllib.error.URLError, TimeoutError) as e:
            return CheckResult(
                name=self.name,
                status=CheckStatus.fail,
                message=f"unreachable at {base}: {e}",
            )


class NotificationsConfiguredCheck:
    """Notifications enabled implies a usable transport + topic.

    Doesn't actually push — just verifies config coherence."""

    name = "notifications"

    def run(self, ctx: DoctorContext) -> CheckResult:
        n_enabled = _settings_attr(ctx.settings, "notifications", "enabled", default=False)
        n_transport = _settings_attr(ctx.settings, "notifications", "transport", default="none")
        n_topic = _settings_attr(ctx.settings, "notifications", "ntfy_topic", default=None)
        if not n_enabled:
            return CheckResult(name=self.name, status=CheckStatus.skipped, message="disabled")
        if n_transport == "ntfy" and not n_topic:
            return CheckResult(
                name=self.name,
                status=CheckStatus.fail,
                message="enabled+transport=ntfy but ntfy_topic is empty",
            )
        return CheckResult(
            name=self.name,
            status=CheckStatus.ok,
            message=f"enabled, transport={n_transport}",
        )


class IdentityCheck:
    """An IdentityManager bootstrap path resolves (Sprint 7a)."""

    name = "identity"

    def run(self, ctx: DoctorContext) -> CheckResult:
        # We don't materialize the IdentityManager here (that requires a
        # SecretStore); we just verify the module imports cleanly so any
        # config-time wiring errors surface.
        try:
            from agent_core.identity import IdentityManager  # noqa: F401
            from agent_core.secrets import default_store  # noqa: F401

            return CheckResult(
                name=self.name, status=CheckStatus.ok, message="identity module available"
            )
        except Exception as e:
            return CheckResult(
                name=self.name, status=CheckStatus.fail, message=f"identity import failed: {e}"
            )


# ── Doctor ─────────────────────────────────────────────────────────────────


DEFAULT_CHECKS: list[type[HealthCheck]] = [
    SettingsValidCheck,
    StorageReachableCheck,
    MigrationsAtHeadCheck,
    VaultPathCheck,
    OllamaReachableCheck,
    NotificationsConfiguredCheck,
    IdentityCheck,
]


class Doctor:
    """Run a battery of ``HealthCheck``s and return a ``DoctorReport``.

    The default check set covers everything ``agent_core`` ships. Plugins
    (e.g., dcos-agent's gmail integration) extend by passing additional
    checks to ``__init__`` or via ``add_check()``.
    """

    def __init__(self, checks: list[HealthCheck] | None = None) -> None:
        self.checks: list[HealthCheck] = (
            list(checks) if checks is not None else [c() for c in DEFAULT_CHECKS]
        )

    def add_check(self, check: HealthCheck) -> None:
        self.checks.append(check)

    def run(self, ctx: DoctorContext) -> DoctorReport:
        report = DoctorReport()
        for check in self.checks:
            try:
                result = check.run(ctx)
            except Exception as e:  # defensive — a buggy check shouldn't crash doctor
                logger.exception("doctor check %s raised", check.name)
                result = CheckResult(
                    name=check.name,
                    status=CheckStatus.fail,
                    message=f"check raised: {e}",
                )
            report.results.append(result)
        return report


# ── Helpers ─────────────────────────────────────────────────────────────────


def _settings_attr(settings: object, section: str, key: str, *, default: Any) -> Any:
    """Walk ``settings.<section>.<key>`` defensively. Returns ``default`` if
    either the section or the key is absent.

    Accepts either a bare ``AgentSettings`` or a ``SettingsManager`` (drills
    into ``.settings`` when present). Covers the case where a check runs
    against a partial settings object (e.g., a wizard mid-flight)."""
    target = getattr(settings, "settings", settings)  # SettingsManager → AgentSettings
    sec = getattr(target, section, None)
    if sec is None:
        return default
    return getattr(sec, key, default)


def _alembic_head_revision() -> str | None:
    """Read the latest revision id from the package's migration scripts.

    Returns None if the script directory can't be located (which would
    itself be a doctor-worthy problem, but is handled by the caller as a
    warn rather than a hard failure)."""
    try:
        # The package ships its own alembic.ini-equivalent next to migrations.
        from importlib.resources import files

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        migrations_dir = files("agent_core.state.migrations")
        # Build a minimal Alembic Config in-memory pointing at the bundled
        # script directory. This avoids depending on a particular CWD or
        # alembic.ini location.
        cfg = Config()
        cfg.set_main_option("script_location", str(migrations_dir))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception as e:
        logger.debug("could not determine alembic head: %s", e)
        return None


__all__ = [
    "CheckResult",
    "CheckStatus",
    "DEFAULT_CHECKS",
    "Doctor",
    "DoctorContext",
    "DoctorReport",
    "HealthCheck",
    "IdentityCheck",
    "MigrationsAtHeadCheck",
    "NotificationsConfiguredCheck",
    "OllamaReachableCheck",
    "SettingsValidCheck",
    "StorageReachableCheck",
    "VaultPathCheck",
]
