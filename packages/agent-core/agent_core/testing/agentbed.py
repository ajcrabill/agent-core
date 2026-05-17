"""AgentTestBed — fully-wired in-memory agent for tests.

Construct one in a single line; every component is built via
``from_settings`` so the tests reflect how a real install looks. Components
are exposed as properties (lazy — built on first access).

Two construction shortcuts:

    AgentTestBed.create()
        # all defaults: balanced preset, stub embedding provider, in-memory db,
        # notifications disabled.

    AgentTestBed.create(preset="aggressive")
        # apply a named preset.

    AgentTestBed.create(settings={"learning": {"detector_strictness": "strict"}})
        # explicit overrides applied on top of defaults.

Use ``with_setting()`` for fluent overrides post-construction:

    bed = AgentTestBed.create()
    bed.with_setting("notifications.enabled", True) \
       .with_setting("notifications.ntfy_topic", "test")
"""

from __future__ import annotations

from typing import Any

from agent_core.openbrain.store import OpenBrainStore
from agent_core.settings import AgentSettings, apply_preset
from agent_core.settings.presets import PresetName
from agent_core.state.db import Database


class AgentTestBed:
    """All the agent-core components, wired together, ready to drive in tests."""

    def __init__(self, settings: AgentSettings, db: Database) -> None:
        self.settings = settings
        self.db = db
        # Lazy-built components — only constructed on demand to keep test
        # startup fast and to avoid wiring concerns the test doesn't exercise.
        self._openbrain: OpenBrainStore | None = None
        self._dispatcher = None
        self._calibration = None
        self._maintenance = None
        self._sampling = None
        self._detector = None
        self._digest_builder = None
        self._pipeline_monitor = None

    # ── Constructors ────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        *,
        preset: PresetName | None = None,
        settings: dict[str, Any] | AgentSettings | None = None,
        db: Database | None = None,
    ) -> AgentTestBed:
        """Build a default test bed.

        Args:
            preset: optional preset name to start from (then ``settings`` is
                applied on top).
            settings: dict of overrides (deep-merged) OR a pre-built
                AgentSettings.
            db: optional Database; default is in-memory SQLite with the full
                schema created.

        Defaults are tuned for tests: stub embedding provider (no network),
        notifications disabled, mesh disabled."""
        s = AgentSettings()
        if preset is not None:
            s = apply_preset(s, preset)
        if isinstance(settings, AgentSettings):
            s = settings
        elif isinstance(settings, dict):
            merged = _deep_merge(s.model_dump(), settings)
            s = AgentSettings.model_validate(merged)

        # Default to stub embedding provider so tests don't need Ollama.
        if (
            s.openbrain.embedding_provider == "ollama"
            and preset is None
            and not isinstance(settings, AgentSettings)
        ):
            s = s.model_copy(
                update={"openbrain": s.openbrain.model_copy(update={"embedding_provider": "stub"})}
            )

        if db is None:
            db = Database.sqlite_memory()
            db.create_all()

        return cls(s, db)

    # ── Component accessors ────────────────────────────────────────────────

    @property
    def openbrain(self) -> OpenBrainStore:
        if self._openbrain is None:
            self._openbrain = OpenBrainStore.from_settings(self.settings, self.db)
        return self._openbrain

    @property
    def dispatcher(self):
        from agent_core.notifications import NotificationDispatcher

        if self._dispatcher is None:
            self._dispatcher = NotificationDispatcher.from_settings(self.settings)
        return self._dispatcher

    @property
    def calibration(self):
        from agent_core.content_creation.calibration import CalibrationManager

        if self._calibration is None:
            self._calibration = CalibrationManager.from_settings(self.settings, self.db)
        return self._calibration

    @property
    def maintenance(self):
        from agent_core.learning.maintenance import MaintenanceScan

        if self._maintenance is None:
            self._maintenance = MaintenanceScan.from_settings(self.settings, self.db)
        return self._maintenance

    @property
    def sampling(self):
        from agent_core.quality.sampling import SamplingPolicy

        if self._sampling is None:
            self._sampling = SamplingPolicy.from_settings(self.settings, self.db)
        return self._sampling

    @property
    def detector(self):
        from agent_core.learning.detector import HeuristicDetector

        if self._detector is None:
            self._detector = HeuristicDetector.from_settings(self.settings)
        return self._detector

    @property
    def digest_builder(self):
        from agent_core.actions.digest import DailyDigestBuilder

        if self._digest_builder is None:
            self._digest_builder = DailyDigestBuilder.from_settings(self.settings, self.db)
        return self._digest_builder

    @property
    def pipeline_monitor(self):
        from agent_core.work.pipeline_monitor import PipelineMonitor

        if self._pipeline_monitor is None:
            self._pipeline_monitor = PipelineMonitor.from_settings(self.settings, self.db)
        return self._pipeline_monitor

    @property
    def learning_store(self):
        from agent_core.learning.store import LearningStore

        return LearningStore(self.db, write_ahead=False)

    @property
    def candidates(self):
        from agent_core.learning.candidates import CorrectionCandidates

        return CorrectionCandidates(self.db)

    # ── Fluent overrides ────────────────────────────────────────────────────

    def with_setting(self, dotted_path: str, value: Any) -> AgentTestBed:
        """Set a setting and reset cached components so they pick up the change.

        Returns ``self`` for chaining."""
        parts = dotted_path.split(".")
        if len(parts) != 2:
            raise ValueError(
                f"with_setting only supports section.field paths (got {dotted_path!r})"
            )
        section_name, field_name = parts
        section = getattr(self.settings, section_name)
        new_section = section.model_copy(update={field_name: value})
        self.settings = self.settings.model_copy(update={section_name: new_section})
        # Invalidate every cached component — next access rebuilds with new settings.
        for attr in (
            "_openbrain",
            "_dispatcher",
            "_calibration",
            "_maintenance",
            "_sampling",
            "_detector",
            "_digest_builder",
            "_pipeline_monitor",
        ):
            setattr(self, attr, None)
        return self


# ── Helpers ─────────────────────────────────────────────────────────────────


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


__all__ = ["AgentTestBed"]
