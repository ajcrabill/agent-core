"""agent_core.settings — single source of truth for user-tunable knobs.

All configuration that the wizard / CLI / user can change lives here. Three
resolution layers, applied in order:

    1. Defaults baked into the Pydantic schema (``schema.py``).
    2. ``agent.yml`` in the agent's data dir (what the wizard writes; what
       users edit).
    3. Environment variables ``AGENT_<SECTION>__<KEY>=<VALUE>``
       (e.g. ``AGENT_AUTONOMY__DEFAULT_POLICY=cautious``).

Each value tracks its source so ``agent settings show`` can tell the user
*why* a value is what it is.

Three named presets ship in ``presets.py`` so non-technical users don't have
to think field-by-field:

    cautious    — everything gated, notifications off, learning loose
    balanced    — green skills autonomous, critical notifications, learning balanced
    aggressive  — most things autonomous, all notifications, learning strict
"""

from agent_core.settings.manager import SettingsManager, SettingsSource, ValueWithSource
from agent_core.settings.presets import PRESETS, apply_preset, list_presets
from agent_core.settings.schema import (
    AgentSettings,
    AutonomySettings,
    EmailIMAPSettings,
    EmailSettings,
    LLMSettings,
    LearningSettings,
    MeshSettings,
    NotificationSettings,
    OpenBrainSettings,
    QualitySettings,
    RuntimeSettings,
    StorageSettings,
    WorkSettings,
)

__all__ = [
    "PRESETS",
    "AgentSettings",
    "AutonomySettings",
    "EmailIMAPSettings",
    "EmailSettings",
    "LLMSettings",
    "LearningSettings",
    "MeshSettings",
    "NotificationSettings",
    "OpenBrainSettings",
    "QualitySettings",
    "RuntimeSettings",
    "SettingsManager",
    "SettingsSource",
    "StorageSettings",
    "ValueWithSource",
    "WorkSettings",
    "apply_preset",
    "list_presets",
]
