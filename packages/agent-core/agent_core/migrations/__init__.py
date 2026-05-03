"""agent_core.migrations — one-shot migration tools.

Each module here reads from a *different* legacy source format and emits a
backup-format JSON dict that ``agent_core.ops.restore_backup`` accepts
without modification. That makes the migration → install workflow trivial:

    1. Run the migration tool on the source data → produces backup.json
    2. Install the target product (dcos-agent / ikb-agent) on a fresh user
    3. Run ``dcos restore backup.json`` (or call ``restore_backup`` directly)

Built-in migrations:

  - ``from_loriah_vault`` — pulls the Loriah Obsidian-vault markdown files
    (operational-state.md, conversation-journal.md, learning-log-data.md)
    into Thought rows + seed Obligations.

  - ``from_esby_install`` — pulls Esby's ``installed-chief-of-staff/``
    sqlite + YAML configs into Person rows, LearningRules (translated
    from Esby's policy_rules), and Thoughts (configs + setup-report).

Future migrations follow the same shape; each gets its own module + test
file + entry in the migration CLI. Shared internals live in ``_helpers.py``.
"""

from agent_core.migrations.from_esby_install import (
    EsbyInstallMigration,
    migrate_esby_install,
)
from agent_core.migrations.from_esby_install import (
    MigratedState as EsbyMigratedState,
)
from agent_core.migrations.from_esby_install import (
    to_backup_payload as esby_to_backup_payload,
)
from agent_core.migrations.from_loriah_vault import (
    LoriahVaultMigration,
    MigratedState,
    migrate_loriah_vault,
    to_backup_payload,
)

__all__ = [
    "EsbyInstallMigration",
    "EsbyMigratedState",
    "LoriahVaultMigration",
    "MigratedState",
    "esby_to_backup_payload",
    "migrate_esby_install",
    "migrate_loriah_vault",
    "to_backup_payload",
]
