"""ikb-agent — team intelligent knowledge base.

Small-team product built on `agent-core`. Default backend: PostgreSQL + pgvector.
Default skills (Sprint 11.5): librarian, knowledge-harvester, document-creator,
email-composer, newsletter-creator, meeting-evaluator, decision-matrix,
confidence-scoring.

End users interact through the ``ikb`` CLI (see ``ikb_agent.cli``); the
SDK surface re-exports the most common agent-core building blocks under
this namespace so skill packages don't have to import from two places.
"""

__version__ = "0.0.1"

from agent_core.notifications import (
    Notification,
    NotificationDispatcher,
    Urgency,
)
from agent_core.openbrain import OpenBrainStore
from agent_core.settings import AgentSettings, SettingsManager
from agent_core.state import Database

from ikb_agent.defaults import (
    INSTANCE_NAME,
    config_dir,
    default_db_url,
    default_settings_path,
    state_dir,
)

__all__ = [
    "AgentSettings",
    "Database",
    "INSTANCE_NAME",
    "Notification",
    "NotificationDispatcher",
    "OpenBrainStore",
    "SettingsManager",
    "Urgency",
    "config_dir",
    "default_db_url",
    "default_settings_path",
    "state_dir",
]
