"""agent_core.ops — operational surface for installed agents.

Three things live here:

  - **doctor**: a battery of health checks the user can run any time
    (``agent doctor``) to confirm the install is healthy. Each check is
    skippable when the relevant feature isn't configured, so a minimal
    install reports "ok" without fighting the user about Ollama or mesh
    peers they don't use.

  - **backup / restore**: portable point-in-time export of agent state
    (db rows + agent.yml + identity bundle, optionally vault). Backups
    are versioned and validated against the current schema on restore.

  - **wizard**: 3-tier interview-style setup. Tier 1 = the smallest set of
    questions that yields a working install. Tier 2 = integrations + push.
    Tier 3 = every settings knob with explanations.

These are intentionally CLI-only. Web UI (OpenWebUI) wraps them later.
"""

from agent_core.ops.backup import (
    BackupFormatError,
    BackupManifest,
    create_backup,
    read_backup,
    write_backup,
)
from agent_core.ops.doctor import (
    CheckResult,
    CheckStatus,
    Doctor,
    DoctorReport,
    HealthCheck,
)
from agent_core.ops.restore import (
    RestoreError,
    RestoreNotConfirmedError,
    RestoreReport,
    RestoreSchemaMismatchError,
    restore_backup,
)
from agent_core.ops.wizard import (
    SetupWizard,
    WizardIO,
    WizardResult,
    WizardValidationError,
    dict_io,
    stdio_io,
)

__all__ = [
    "BackupFormatError",
    "BackupManifest",
    "CheckResult",
    "CheckStatus",
    "Doctor",
    "DoctorReport",
    "HealthCheck",
    "RestoreError",
    "RestoreNotConfirmedError",
    "RestoreReport",
    "RestoreSchemaMismatchError",
    "SetupWizard",
    "WizardIO",
    "WizardResult",
    "WizardValidationError",
    "create_backup",
    "dict_io",
    "read_backup",
    "restore_backup",
    "stdio_io",
    "write_backup",
]
