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

import logging
import os
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


# ── Default selection ────────────────────────────────────────────────────────


def default_store() -> SecretStore:
    """Return the right backend for this environment.

    Tries KeyringSecretStore; falls back to EnvSecretStore on systems where
    keyring can't initialize (e.g., headless Linux without Secret Service).
    """
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        kr = keyring.get_keyring()
        if isinstance(kr, FailKeyring):
            logger.warning(
                "no usable OS keychain found; falling back to EnvSecretStore "
                "(set AGENTCORE_<NAMESPACE>_<KEY> env vars to configure secrets)"
            )
            return EnvSecretStore()
        return KeyringSecretStore()
    except ImportError:
        return EnvSecretStore()


__all__ = [
    "EnvSecretStore",
    "KeyringSecretStore",
    "MemorySecretStore",
    "SecretNotWritableError",
    "SecretStore",
    "default_store",
]
