"""Pluggable secret storage.

Per L11/L17: secrets live in OS keychain by default. Headless servers (CI,
remote install scripts) can fall back to env vars. Tests use the in-memory
backend.

Backends:
  KeyringSecretStore — Python ``keyring`` library; uses macOS Keychain on
                       Mac, Secret Service / libsecret on Linux, Windows
                       Credential Locker on Windows. Default in production.

  EnvSecretStore     — read-only; pulls from environment variables. Good
                       for headless / Docker / CI deployments. Naming:
                       ``AGENTCORE_<NAMESPACE>_<KEY>`` (uppercase, dashes
                       replaced with underscores).

  MemorySecretStore  — in-process dict. Used by tests + ephemeral processes.

API:
  store.get(namespace, key) → str | None
  store.set(namespace, key, value)
  store.delete(namespace, key)
  store.list(namespace) → list[str]   (keys only, not values)

Namespacing convention:
  - "identity"      — agent's own identity secrets (ed25519 seed)
  - "providers"     — third-party API keys (anthropic, openai, deepseek, …)
  - "oauth"         — Gmail/Calendar OAuth tokens
  - "mesh"          — per-peer mesh API keys
  - "<skill-name>"  — skill-specific credentials
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────────


class SecretNotWritableError(RuntimeError):
    """Raised when a read-only backend (e.g., EnvSecretStore) is asked to
    set or delete."""


# ── Protocol ─────────────────────────────────────────────────────────────────


@runtime_checkable
class SecretStore(Protocol):
    """Pluggable secrets backend.

    All methods accept namespace + key as plain strings. Backends namespace
    the underlying storage so collisions across users / installs are
    impossible.
    """

    def get(self, namespace: str, key: str) -> str | None: ...

    def set(self, namespace: str, key: str, value: str) -> None: ...

    def delete(self, namespace: str, key: str) -> None: ...

    def list(self, namespace: str) -> list[str]: ...


# ── Memory (tests) ───────────────────────────────────────────────────────────


class MemorySecretStore:
    """In-process dict. Used by tests + ephemeral processes."""

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], str] = {}

    def get(self, namespace: str, key: str) -> str | None:
        return self._d.get((namespace, key))

    def set(self, namespace: str, key: str, value: str) -> None:
        self._d[(namespace, key)] = value

    def delete(self, namespace: str, key: str) -> None:
        self._d.pop((namespace, key), None)

    def list(self, namespace: str) -> list[str]:
        return sorted(k for (ns, k) in self._d if ns == namespace)


# ── Environment variables (read-only) ────────────────────────────────────────


class EnvSecretStore:
    """Read-only backend reading from os.environ.

    Variable name format: ``AGENTCORE_<NAMESPACE>_<KEY>`` with dashes
    replaced by underscores and the whole thing uppercased.

    Examples:
      ("providers", "anthropic_api_key") → AGENTCORE_PROVIDERS_ANTHROPIC_API_KEY
      ("mesh", "ikb-bob")                → AGENTCORE_MESH_IKB_BOB

    set/delete raise SecretNotWritableError. list() scans environ for
    matching prefix.
    """

    PREFIX = "AGENTCORE_"

    @classmethod
    def _env_var_name(cls, namespace: str, key: str) -> str:
        ns = namespace.replace("-", "_").upper()
        k = key.replace("-", "_").upper()
        return f"{cls.PREFIX}{ns}_{k}"

    @classmethod
    def _env_prefix(cls, namespace: str) -> str:
        ns = namespace.replace("-", "_").upper()
        return f"{cls.PREFIX}{ns}_"

    def get(self, namespace: str, key: str) -> str | None:
        return os.environ.get(self._env_var_name(namespace, key))

    def set(self, namespace: str, key: str, value: str) -> None:
        raise SecretNotWritableError("EnvSecretStore is read-only")

    def delete(self, namespace: str, key: str) -> None:
        raise SecretNotWritableError("EnvSecretStore is read-only")

    def list(self, namespace: str) -> list[str]:
        prefix = self._env_prefix(namespace)
        keys = []
        for env_name in os.environ:
            if env_name.startswith(prefix):
                # Strip the prefix; lowercase; convert _ back to - for
                # display? Convention is the OTHER direction was lossy
                # (- → _), so we surface as lowercase underscored keys.
                keys.append(env_name[len(prefix) :].lower())
        return sorted(keys)


# ── OS keychain (default in production) ──────────────────────────────────────


class KeyringSecretStore:
    """OS keychain via the Python ``keyring`` library.

    Service name format: ``agent-core/<namespace>``. Username is the key.
    On macOS this lands in Keychain; on Linux in Secret Service / libsecret;
    on Windows in Credential Locker.

    list() relies on ``keyring.get_credential`` which is supported on most
    backends but may return empty on backends that don't implement
    enumeration. We track separately the set of keys ever written via this
    instance — that's the falling-back strategy when the backend doesn't
    enumerate.
    """

    SERVICE_PREFIX = "agent-core/"

    def __init__(self) -> None:
        # Local index of keys we know about — supplements keyring backends
        # that don't support enumeration.
        self._known: dict[str, set[str]] = {}

    @classmethod
    def _service_name(cls, namespace: str) -> str:
        return f"{cls.SERVICE_PREFIX}{namespace}"

    def get(self, namespace: str, key: str) -> str | None:
        import keyring

        return keyring.get_password(self._service_name(namespace), key)

    def set(self, namespace: str, key: str, value: str) -> None:
        import keyring

        keyring.set_password(self._service_name(namespace), key, value)
        self._known.setdefault(namespace, set()).add(key)

    def delete(self, namespace: str, key: str) -> None:
        import contextlib

        import keyring
        import keyring.errors

        # Idempotent: deleting a non-existent key is a no-op.
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(self._service_name(namespace), key)
        self._known.get(namespace, set()).discard(key)

    def list(self, namespace: str) -> list[str]:
        return sorted(self._known.get(namespace, set()))


# ── File-backed (writable Linux/headless fallback) ──────────────────────────


class FileSecretStore:
    """JSON-file backend at ``~/.local/state/agent-core/secrets.json``.

    Used when no usable OS keychain is available — typical for headless
    VPS installs (Hostinger, DigitalOcean, etc.) without Secret Service /
    GNOME Keyring. Writable, unlike EnvSecretStore.

    Format::

        {
          "namespace1": {"key1": "value1", "key2": "value2"},
          ...
        }

    Atomic writes via tempfile + os.replace + chmod 0600. Honors
    ``AGENTCORE_SECRETS_PATH`` env var for non-default locations.
    """

    DEFAULT_PATH = Path.home() / ".local" / "state" / "agent-core" / "secrets.json"

    def __init__(self, path: Path | None = None) -> None:
        env_path = os.environ.get("AGENTCORE_SECRETS_PATH")
        self.path = path or (Path(env_path) if env_path else self.DEFAULT_PATH)

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("secrets file unreadable at %s: %s", self.path, e)
            return {}

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        import tempfile

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.path.parent,
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp_path = Path(tmp.name)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self.path)

    def get(self, namespace: str, key: str) -> str | None:
        return self._read().get(namespace, {}).get(key)

    def set(self, namespace: str, key: str, value: str) -> None:
        data = self._read()
        data.setdefault(namespace, {})[key] = value
        self._write(data)

    def delete(self, namespace: str, key: str) -> None:
        data = self._read()
        ns = data.get(namespace, {})
        if key in ns:
            del ns[key]
            if not ns:
                del data[namespace]
            self._write(data)

    def list(self, namespace: str) -> list[str]:
        return sorted(self._read().get(namespace, {}).keys())


# ── Default selection ────────────────────────────────────────────────────────


_PROBE_NS = "_agent_core_probe"
_PROBE_KEY = "writable"


def _keyring_is_writable() -> bool:
    """Probe the OS keychain by performing a no-op write/delete cycle.

    On macOS over SSH (and other GUI-session-only configurations), the
    keychain backend reports as "available" but every actual write fails
    with -60008. The fail backend isn't selected by keyring's auto-pick;
    we have to actually try a write. Probe-and-fail is much cheaper than
    failing during init and confusing the user.

    Returns True only when both set and delete succeed. Any exception is
    treated as "not writable".
    """
    try:
        import keyring

        keyring.set_password(
            f"{KeyringSecretStore.SERVICE_PREFIX}{_PROBE_NS}",
            _PROBE_KEY,
            "1",
        )
        import contextlib

        with contextlib.suppress(Exception):
            keyring.delete_password(
                f"{KeyringSecretStore.SERVICE_PREFIX}{_PROBE_NS}",
                _PROBE_KEY,
            )
        return True
    except Exception as e:
        logger.info("keychain not writable in this session: %s", e)
        return False


def default_store() -> SecretStore:
    """Return the right backend for this environment.

    Resolution order:
      1. ``KeyringSecretStore`` — OS keychain, but only after a write
         probe confirms it actually works in this session. macOS over
         SSH advertises the keychain backend as available but rejects
         writes with -60008; the probe catches that without leaving the
         user with an init that prints "could not store token".
      2. ``FileSecretStore`` — writable JSON file at
         ``~/.local/state/agent-core/secrets.json`` (mode 0600). Used
         on headless Linux installs and macOS-over-SSH.
      3. ``EnvSecretStore`` — read-only fallback; only if neither of the
         above is usable.

    The file fallback was added in Sprint 15g; the write-probe was added
    after observing macOS-over-SSH installs failing keychain writes
    silently (Sprint 24-era real-world install runs).

    Honors ``AGENTCORE_SECRETS_BACKEND=file`` to skip the probe and force
    file storage — useful for power users automating headless installs.
    """
    if os.environ.get("AGENTCORE_SECRETS_BACKEND") == "file":
        return FileSecretStore()
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        kr = keyring.get_keyring()
        if isinstance(kr, FailKeyring):
            logger.info(
                "no usable OS keychain backend; using FileSecretStore at %s",
                FileSecretStore.DEFAULT_PATH,
            )
            return FileSecretStore()
        if not _keyring_is_writable():
            logger.info(
                "keychain not writable (likely macOS over SSH); using FileSecretStore at %s",
                FileSecretStore.DEFAULT_PATH,
            )
            return FileSecretStore()
        return KeyringSecretStore()
    except ImportError:
        return FileSecretStore()


__all__ = [
    "EnvSecretStore",
    "FileSecretStore",
    "KeyringSecretStore",
    "MemorySecretStore",
    "SecretNotWritableError",
    "SecretStore",
    "default_store",
]
