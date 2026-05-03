"""SettingsManager — load, resolve, and persist agent settings.

Resolution layers (low → high precedence):

    1. Defaults baked into the Pydantic schema.
    2. ``agent.yml`` in the agent's data dir (if present).
    3. Environment variables ``AGENT_<SECTION>__<KEY>`` (double underscore
       between section and key — pydantic-settings convention).

The manager keeps track of *where* each value came from so the CLI can
explain what's set and why.

File format (``agent.yml``):

    autonomy:
      default_policy: cautious
      per_action_overrides:
        send_email_external: gated
    learning:
      detector_strictness: strict
    notifications:
      enabled: true
      ntfy_topic: my-private-topic-7x9k

Saved files are written atomically (write-temp-then-rename) so a crash
mid-write never corrupts the user's config.

Use ``settings.get("autonomy.default_policy")`` and
``settings.set("autonomy.default_policy", "aggressive")`` for CLI-friendly
dotted-path access.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_core.settings.presets import apply_preset
from agent_core.settings.schema import AgentSettings, PresetName

logger = logging.getLogger(__name__)

ENV_PREFIX = "AGENT_"
ENV_NESTED_DELIMITER = "__"
DEFAULT_FILENAME = "agent.yml"


class SettingsSource(StrEnum):
    """Where a particular value came from."""

    default = "default"
    file = "file"
    env = "env"


@dataclass(frozen=True)
class ValueWithSource:
    """A resolved setting value plus the layer that won."""

    path: str
    value: Any
    source: SettingsSource


# ── Manager ─────────────────────────────────────────────────────────────────


class SettingsManager:
    """Load, mutate, persist ``AgentSettings`` with source tracking.

    Args:
        path: ``agent.yml`` location. If None, defaults to
              ``$AGENT_DATA_DIR/agent.yml`` (or ``./agent.yml`` if unset).
        env: Mapping to read env vars from. Default: ``os.environ``. Tests
             pass a fresh dict for isolation.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.path = Path(path) if path else _default_path()
        self._env = env if env is not None else os.environ
        self._settings: AgentSettings = AgentSettings()  # defaults
        self._sources: dict[str, SettingsSource] = {}
        self.reload()

    # ── Loading ─────────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-read ``agent.yml`` + env vars and rebuild resolved settings."""
        defaults_dump = AgentSettings().model_dump()
        file_overlay = self._read_file()
        env_overlay = self._read_env()

        merged = _deep_merge(defaults_dump, file_overlay)
        merged = _deep_merge(merged, env_overlay)

        try:
            self._settings = AgentSettings.model_validate(merged)
        except ValidationError as e:
            raise SettingsError(
                f"resolved settings failed validation:\n{e}\n"
                f"check {self.path} and AGENT_* env vars"
            ) from e

        self._sources = _compute_sources(defaults_dump, file_overlay, env_overlay)

    # ── Read API ────────────────────────────────────────────────────────────

    @property
    def settings(self) -> AgentSettings:
        """The fully-resolved ``AgentSettings``. Treat as read-only — use
        ``.set()`` / ``.save()`` to mutate persistently."""
        return self._settings

    def get(self, dotted_path: str) -> Any:
        """Get a value by ``section.field`` path. Raises KeyError if unknown."""
        node: Any = self._settings.model_dump()
        for part in dotted_path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"unknown setting: {dotted_path!r}")
            node = node[part]
        return node

    def get_with_source(self, dotted_path: str) -> ValueWithSource:
        """Get value + which layer (default/file/env) supplied it."""
        return ValueWithSource(
            path=dotted_path,
            value=self.get(dotted_path),
            source=self._sources.get(dotted_path, SettingsSource.default),
        )

    def all_with_sources(self) -> list[ValueWithSource]:
        """Snapshot every leaf value with its source — for ``settings show``."""
        out: list[ValueWithSource] = []
        for path, value in _walk_leaves(self._settings.model_dump()):
            out.append(
                ValueWithSource(
                    path=path,
                    value=value,
                    source=self._sources.get(path, SettingsSource.default),
                )
            )
        return out

    # ── Mutation ────────────────────────────────────────────────────────────

    def set(self, dotted_path: str, value: Any, *, save: bool = True) -> None:
        """Set a single value (validating against the schema), then persist
        to ``agent.yml`` unless ``save=False``."""
        file_state = self._read_file()
        _set_path(file_state, dotted_path, value)

        merged = _deep_merge(AgentSettings().model_dump(), file_state)
        try:
            AgentSettings.model_validate(merged)
        except ValidationError as e:
            raise SettingsError(f"value rejected by schema for {dotted_path!r}: {e}") from e

        if save:
            self._write_file(file_state)
        self.reload()

    def reset(self, dotted_path: str | None = None, *, save: bool = True) -> None:
        """Remove a per-key override (or *all* overrides if ``dotted_path`` is
        None) and rewrite the file."""
        if dotted_path is None:
            file_state: dict[str, Any] = {}
        else:
            file_state = self._read_file()
            _delete_path(file_state, dotted_path)
        if save:
            self._write_file(file_state)
        self.reload()

    def apply_preset(self, name: PresetName, *, save: bool = True) -> None:
        """Overlay a named preset onto current settings + persist."""
        new_state = apply_preset(self._settings, name)
        # Persist the *full resolved* state minus pure defaults — i.e., diff
        # against schema defaults so we don't bloat agent.yml with values
        # that match the schema.
        diff = _diff_against_defaults(new_state.model_dump(), AgentSettings().model_dump())
        if save:
            self._write_file(diff)
        self.reload()

    # ── File I/O ────────────────────────────────────────────────────────────

    def _read_file(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
        except yaml.YAMLError as e:
            raise SettingsError(f"{self.path}: invalid YAML: {e}") from e
        if not isinstance(data, dict):
            raise SettingsError(f"{self.path}: top-level must be a mapping, got {type(data).__name__}")
        return data

    def _write_file(self, data: dict[str, Any]) -> None:
        """Atomic write — write to a tempfile in the same dir, then rename.
        A crash mid-write leaves the original ``agent.yml`` intact."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=self.path.parent,
            prefix=self.path.name + ".",
            suffix=".tmp",
        ) as tmp:
            yaml.safe_dump(data, tmp, sort_keys=True, default_flow_style=False)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, self.path)

    # ── Env parsing ─────────────────────────────────────────────────────────

    def _read_env(self) -> dict[str, Any]:
        """Parse ``AGENT_<SECTION>__<KEY>`` env vars into a nested dict.

        Type coercion is intentionally minimal: we only convert "true"/"false"
        to bool and numeric strings to int/float. Pydantic does the rest at
        validation time."""
        overlay: dict[str, Any] = {}
        for raw_key, raw_val in self._env.items():
            if not raw_key.startswith(ENV_PREFIX):
                continue
            tail = raw_key[len(ENV_PREFIX) :]
            if ENV_NESTED_DELIMITER not in tail:
                continue  # AGENT_FOO with no section is ignored — avoids collisions
            parts = tail.lower().split(ENV_NESTED_DELIMITER)
            _set_path(overlay, ".".join(parts), _coerce_env(raw_val))
        return overlay


# ── Errors ──────────────────────────────────────────────────────────────────


class SettingsError(Exception):
    """Raised on invalid file content, invalid env vars, or rejected mutations."""


# ── Helpers ─────────────────────────────────────────────────────────────────


def _default_path() -> Path:
    """Default ``agent.yml`` location.

    Honors ``AGENT_DATA_DIR`` if set; otherwise uses the current working
    directory. The wizard typically writes ``AGENT_DATA_DIR`` to a per-agent
    location (e.g., ``~/.config/dcos-agent``)."""
    base = os.environ.get("AGENT_DATA_DIR")
    return Path(base) / DEFAULT_FILENAME if base else Path.cwd() / DEFAULT_FILENAME


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _walk_leaves(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten a nested dict to (dotted-path, leaf-value) pairs."""
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(_walk_leaves(v, f"{prefix}.{k}" if prefix else k))
    else:
        out.append((prefix, node))
    return out


def _set_path(d: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _delete_path(d: dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            return
        cur = cur[p]
    cur.pop(parts[-1], None)


def _coerce_env(raw: str) -> Any:
    """Light-touch env-var coercion."""
    lo = raw.strip().lower()
    if lo in ("true", "yes", "on"):
        return True
    if lo in ("false", "no", "off"):
        return False
    if lo == "null" or lo == "none" or lo == "":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _compute_sources(
    defaults: dict[str, Any],
    file_overlay: dict[str, Any],
    env_overlay: dict[str, Any],
) -> dict[str, SettingsSource]:
    """For each leaf, decide which layer supplied the winning value."""
    sources: dict[str, SettingsSource] = {}
    for path, _val in _walk_leaves(defaults):
        sources[path] = SettingsSource.default
    for path, _val in _walk_leaves(file_overlay):
        sources[path] = SettingsSource.file
    for path, _val in _walk_leaves(env_overlay):
        sources[path] = SettingsSource.env
    return sources


def _diff_against_defaults(
    current: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Return only the keys in ``current`` that differ from ``defaults`` —
    so ``agent.yml`` stays minimal."""
    out: dict[str, Any] = {}
    for k, v in current.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            sub = _diff_against_defaults(v, defaults[k])
            if sub:
                out[k] = sub
        else:
            if defaults.get(k) != v:
                out[k] = v
    return out


__all__ = [
    "SettingsError",
    "SettingsManager",
    "SettingsSource",
    "ValueWithSource",
]
