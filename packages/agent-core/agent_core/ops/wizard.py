"""3-tier interview-style setup wizard.

Three tiers, each strictly a superset of the previous one. The user picks
their depth at the start; we never force anyone to answer fifty questions
to get a working install.

  Tier 1 — *minimum viable*. ~3 questions. Yields a usable install.
    - Display name
    - Data dir (where agent.yml + sqlite db live)
    - Preset: cautious | balanced | aggressive

  Tier 2 — *integrations + push*. ~7 questions, layered on Tier 1.
    - Notifications: enable + ntfy_topic + urgency_floor
    - Vault path (optional — for Obsidian-style round-tripping)
    - Embedding provider: ollama | stub
    - Mesh: enable peering with another agent? (optional)

  Tier 3 — *every knob*. Walks the full settings schema field-by-field
    with the schema-defined description, current value, and default. Long;
    only for power users who want to dial everything in up front.

Implementation notes:
    - Pure Python — no Click. The CLI wraps it; tests can drive it with
      pre-canned answer dicts.
    - The wizard never makes filesystem changes itself except the final
      ``commit()`` step, which writes ``agent.yml``. This keeps interrupts
      safe.
    - All input is validated against the settings schema *before* commit,
      so a typo in tier 1 doesn't leave you with a half-written file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from agent_core.settings import (
    AgentSettings,
    SettingsManager,
    apply_preset,
)
from agent_core.settings.presets import list_presets
from agent_core.settings.schema import PresetName

logger = logging.getLogger(__name__)


WizardTier = Literal[1, 2, 3]


# ── I/O Protocol ───────────────────────────────────────────────────────────


@dataclass
class WizardIO:
    """How the wizard talks to the user.

    Default implementation prints + reads stdin (driven by the CLI). Tests
    pass a pre-canned answer dict via ``DictIO`` to avoid stdin altogether."""

    ask: Callable[[str, str | None], str]
    """ask(prompt, default) → user's answer (or default if they hit return)."""
    confirm: Callable[[str, bool], bool]
    """confirm(prompt, default) → bool."""
    say: Callable[[str], None]
    """say(line) → display informational text."""


def stdio_io() -> WizardIO:
    """Default WizardIO: stdin/stdout. Used by the CLI."""

    def _ask(prompt: str, default: str | None) -> str:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        return raw if raw else (default or "")

    def _confirm(prompt: str, default: bool) -> bool:
        d = "Y/n" if default else "y/N"
        raw = input(f"{prompt} [{d}]: ").strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes")

    def _say(line: str) -> None:
        print(line)

    return WizardIO(ask=_ask, confirm=_confirm, say=_say)


def dict_io(answers: dict[str, Any]) -> WizardIO:
    """Test-friendly WizardIO. Each ``ask`` looks up its prompt-key in
    ``answers``; missing keys fall back to the default. Useful for driving
    the wizard non-interactively in tests + scripts."""

    def _ask(prompt: str, default: str | None) -> str:
        return str(answers.get(prompt, default or ""))

    def _confirm(prompt: str, default: bool) -> bool:
        v = answers.get(prompt, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("y", "yes", "true", "on")
        return default

    def _say(_line: str) -> None:
        pass

    return WizardIO(ask=_ask, confirm=_confirm, say=_say)


# ── Result type ────────────────────────────────────────────────────────────


@dataclass
class WizardResult:
    """What the wizard decided. Caller commits to disk via ``commit()``."""

    tier: WizardTier
    settings: AgentSettings
    overrides: dict[str, Any] = field(default_factory=dict)
    """Flat dict of dotted-path → value, for diff display."""

    def commit(self, path: Path) -> None:
        """Persist to ``agent.yml`` (atomic, schema-validated).

        Caller-owned pseudo-keys (``__display_name`` etc., names starting
        with ``__``) are not persisted to settings — they're just carried
        in the result for the caller to consume separately."""
        mgr = SettingsManager(path=path, env={})
        for dotted, value in self.overrides.items():
            if dotted.startswith("__"):
                continue
            mgr.set(dotted, value, save=False)
        mgr._write_file(
            _diff_against_defaults(  # type: ignore[attr-defined]
                self.settings.model_dump(), AgentSettings().model_dump()
            )
        )


def _diff_against_defaults(current: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Same as the SettingsManager helper — duplicated to keep wizard
    self-contained (tiny + the import would create a private-API entanglement)."""
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


# ── Wizard ─────────────────────────────────────────────────────────────────


class SetupWizard:
    """Drive an interview to produce an ``AgentSettings`` + an ``agent.yml`` location."""

    def __init__(
        self,
        io: WizardIO | None = None,
        *,
        default_db_urls: dict[str, str] | None = None,
    ) -> None:
        """Build a wizard.

        ``default_db_urls`` is an optional mapping from backend name
        ("sqlite" / "postgres") to a backend-appropriate URL the product
        wants written into settings.storage.url when the user picks that
        backend. Without it, the wizard sets only storage.backend and
        leaves storage.url at the schema default — fine for tests, but
        downstream init will fall back to a cwd-relative SQLite path
        which probably isn't what the user wanted.

        Pass ``{"sqlite": "sqlite:///<state-dir>/agent.db", "postgres":
        "postgresql+psycopg:///ikb_agent?host=/tmp"}`` from the product
        setup wrapper so the wizard's choice flows through to disk.
        """
        self.io = io or stdio_io()
        self.default_db_urls = default_db_urls or {}

    # ── Entry ──────────────────────────────────────────────────────────────

    def run(self, tier: WizardTier = 1) -> WizardResult:
        """Run the chosen tier (and all lower tiers in sequence)."""
        if tier not in (1, 2, 3):
            raise ValueError(f"tier must be 1, 2, or 3 (got {tier!r})")

        overrides: dict[str, Any] = {}
        settings = AgentSettings()

        # Tier 1
        settings, overrides = self._tier_1(settings, overrides)

        if tier >= 2:
            settings, overrides = self._tier_2(settings, overrides)
        if tier >= 3:
            settings, overrides = self._tier_3(settings, overrides)

        return WizardResult(tier=tier, settings=settings, overrides=overrides)

    # ── Tier 1: minimum viable ────────────────────────────────────────────

    def _tier_1(
        self, settings: AgentSettings, overrides: dict[str, Any]
    ) -> tuple[AgentSettings, dict[str, Any]]:
        self.io.say("Tier 1 — three questions to a working agent.")

        # 1. Preset (drives most other defaults)
        valid = list_presets()
        chosen = self.io.ask(
            f"Choose a preset ({'|'.join(valid)})",
            default="balanced",
        )
        if chosen not in valid:
            raise WizardValidationError(f"unknown preset {chosen!r}; expected one of {valid}")
        preset_name: PresetName = chosen  # type: ignore[assignment]
        settings = apply_preset(settings, preset_name)
        # Preset overrides are recorded as a single virtual key so commit()
        # can later compute the file diff; we do NOT enumerate every preset
        # field as an override here.

        # 2. Display name (lives in identity, not settings — but we record
        # it for the wizard's output so the caller knows what to seed.)
        display_name = self.io.ask("Your display name", default="").strip()
        if display_name:
            overrides["__display_name"] = display_name  # caller-owned, not in settings

        # 3. Storage backend (sqlite vs postgres). Most installs stay sqlite.
        backend = self.io.ask("Storage backend (sqlite|postgres)", default="sqlite")
        if backend not in ("sqlite", "postgres"):
            raise WizardValidationError(f"backend must be sqlite or postgres (got {backend!r})")
        storage_update: dict[str, Any] = {}
        if backend != settings.storage.backend:
            storage_update["backend"] = backend
            overrides["storage.backend"] = backend
        # Write storage.url when the caller supplied a backend-specific
        # default; this keeps init's `db_url or settings.storage.url`
        # resolution self-consistent with the user's choice and avoids
        # the cwd-relative `sqlite:///./agent.db` schema default leaking
        # through.
        if backend in self.default_db_urls:
            chosen_url = self.default_db_urls[backend]
            if chosen_url and chosen_url != settings.storage.url:
                storage_update["url"] = chosen_url
                overrides["storage.url"] = chosen_url
        if storage_update:
            settings = settings.model_copy(
                update={"storage": settings.storage.model_copy(update=storage_update)}
            )

        return settings, overrides

    # ── Tier 2: integrations + push ───────────────────────────────────────

    def _tier_2(
        self, settings: AgentSettings, overrides: dict[str, Any]
    ) -> tuple[AgentSettings, dict[str, Any]]:
        self.io.say("Tier 2 — integrations + push notifications.")

        # Notifications
        push = self.io.confirm(
            "Enable push notifications via ntfy?", default=settings.notifications.enabled
        )
        if push != settings.notifications.enabled:
            overrides["notifications.enabled"] = push
        new_notifs = settings.notifications.model_copy(update={"enabled": push})

        if push:
            topic = self.io.ask(
                "ntfy topic (pick something unguessable, e.g. 'dcos-7x9k')",
                default=new_notifs.ntfy_topic or "",
            )
            if not topic:
                raise WizardValidationError("ntfy enabled but no topic provided")
            if topic != settings.notifications.ntfy_topic:
                overrides["notifications.ntfy_topic"] = topic
            floor = self.io.ask(
                "Urgency floor (info|warn|critical — only this and above push)",
                default=new_notifs.urgency_floor,
            )
            if floor not in ("info", "warn", "critical"):
                raise WizardValidationError(
                    f"urgency_floor must be info|warn|critical (got {floor!r})"
                )
            if floor != settings.notifications.urgency_floor:
                overrides["notifications.urgency_floor"] = floor
            new_notifs = new_notifs.model_copy(update={"ntfy_topic": topic, "urgency_floor": floor})
        settings = settings.model_copy(update={"notifications": new_notifs})

        # Vault
        vault = self.io.ask("Vault path (Obsidian-style; leave blank to skip)", default="").strip()
        if vault:
            overrides["storage.vault_path"] = vault
            settings = settings.model_copy(
                update={"storage": settings.storage.model_copy(update={"vault_path": vault})}
            )

        # Embedding provider
        provider = self.io.ask(
            "Embedding provider (ollama|stub|stub-semantic)",
            default=settings.openbrain.embedding_provider,
        )
        if provider not in ("ollama", "stub", "stub-semantic"):
            raise WizardValidationError(
                f"embedding_provider must be ollama|stub|stub-semantic (got {provider!r})"
            )
        if provider != settings.openbrain.embedding_provider:
            overrides["openbrain.embedding_provider"] = provider
            settings = settings.model_copy(
                update={
                    "openbrain": settings.openbrain.model_copy(
                        update={"embedding_provider": provider}
                    )
                }
            )

        # Mesh (optional, default off)
        mesh = self.io.confirm(
            "Enable mesh peering with other agents?", default=settings.mesh.enabled
        )
        if mesh != settings.mesh.enabled:
            overrides["mesh.enabled"] = mesh
            settings = settings.model_copy(
                update={"mesh": settings.mesh.model_copy(update={"enabled": mesh})}
            )

        return settings, overrides

    # ── Tier 3: every knob ────────────────────────────────────────────────

    def _tier_3(
        self, settings: AgentSettings, overrides: dict[str, Any]
    ) -> tuple[AgentSettings, dict[str, Any]]:
        self.io.say("Tier 3 — every settings field. Press return to keep the current value.")

        # Walk the schema's leaves with each field's description as the prompt.
        # We skip fields already touched in tiers 1-2 to avoid double-asking.
        already_asked = set(overrides.keys())

        for path, field_info, current in _walk_schema_leaves(settings):
            if path in already_asked:
                continue
            description = (field_info.description or "").strip()
            prompt = f"{path} — {description}" if description else path
            raw = self.io.ask(prompt, default=str(current) if current is not None else "")
            if raw == str(current) or (current is None and raw == ""):
                continue  # unchanged
            value = _parse_for_field(field_info, raw)
            overrides[path] = value
            settings = _apply_dotted(settings, path, value)

        return settings, overrides


# ── Errors ─────────────────────────────────────────────────────────────────


class WizardValidationError(ValueError):
    """User-supplied input failed schema or wizard validation."""


# ── Helpers ─────────────────────────────────────────────────────────────────


def _walk_schema_leaves(settings: AgentSettings):
    """Yield (dotted_path, FieldInfo, current_value) for each leaf field."""
    from pydantic import BaseModel

    def walk(model: BaseModel, prefix: str = ""):
        for fname, finfo in type(model).model_fields.items():
            value = getattr(model, fname)
            path = f"{prefix}.{fname}" if prefix else fname
            if isinstance(value, BaseModel):
                yield from walk(value, path)
            else:
                yield (path, finfo, value)

    yield from walk(settings)


def _parse_for_field(finfo, raw: str) -> Any:
    """Coerce raw user input to the field's type. Best-effort — schema
    validation in commit() catches the rest."""
    annotation = finfo.annotation
    if annotation is bool:
        return raw.strip().lower() in ("y", "yes", "true", "on", "1")
    if annotation is int:
        return int(raw)
    if annotation is float:
        return float(raw)
    return raw


def _apply_dotted(settings: AgentSettings, dotted: str, value: Any) -> AgentSettings:
    """Return a new AgentSettings with one dotted-path field replaced."""
    parts = dotted.split(".")
    section_name = parts[0]
    field_name = ".".join(parts[1:])
    section = getattr(settings, section_name)
    if "." in field_name:
        # Future: deeper nesting. Today's schema is two levels max.
        raise NotImplementedError(f"deep nested set not supported: {dotted}")
    new_section = section.model_copy(update={field_name: value})
    return settings.model_copy(update={section_name: new_section})


__all__ = [
    "DictIO",
    "SetupWizard",
    "WizardIO",
    "WizardResult",
    "WizardTier",
    "WizardValidationError",
    "dict_io",
    "stdio_io",
]


# Backwards-friendly alias for the test-friendly IO helper
DictIO = dict_io
