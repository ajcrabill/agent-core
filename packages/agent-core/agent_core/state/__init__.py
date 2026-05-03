"""agent_core.state — canonical operational state.

This module owns the database schema, dual-backend (sqlite/postgres) connection
management, schema migrations, and the markdown<->db projection layer.

The database is the source of truth. The vault is a generated projection.
"""

from agent_core.state.db import (
    Backend,
    Database,
    default_postgres_dsn,
    default_sqlite_path,
    libpq_dsn_to_sqlalchemy_url,
)
from agent_core.state.models import (
    ActionClass,
    ActionLog,
    ActionOutcome,
    AutonomyOverride,
    Calibration,
    CompletionCheck,
    CorrectionCandidate,
    CorrectionCandidateStatus,
    Delegation,
    DelegationStatus,
    Exemplar,
    # Identity
    Identity,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IngestionRun,
    IntercomAck,
    IntercomMessage,
    IntercomState,
    Iteration,
    IterationStatus,
    # Learning
    LearningRule,
    Metric,
    # Work
    Obligation,
    ObligationEvent,
    ObligationEventKind,
    ObligationOwner,
    ObligationSource,
    ObligationStatus,
    Peer,
    PeerRole,
    Person,
    Plan,
    PlanStatus,
    QualityAudit,
    QualityScore,
    RuleFiring,
    RunLog,
    Session,
    Template,
    Thought,
    ThoughtSource,
    # Helpers
    new_id,
    utcnow,
)
from agent_core.state.renderer import (
    RenderResult,
    VaultRenderer,
    obligation_filename,
    render_learning_rules_md,
    render_obligation_md,
    slugify,
)
from agent_core.state.watcher import (
    ApplyResult,
    ParsedObligation,
    VaultWatcher,
    apply_modified,
    apply_moved,
    column_status,
    is_rendered_obligation_path,
    parse_obligation_md,
)

__all__ = [
    # Database
    "Backend",
    "Database",
    "default_sqlite_path",
    "default_postgres_dsn",
    "libpq_dsn_to_sqlalchemy_url",
    # Identity
    "Identity",
    "Peer",
    "PeerRole",
    # People
    "Person",
    "AutonomyOverride",
    # Work
    "Obligation",
    "ObligationStatus",
    "ObligationOwner",
    "ObligationSource",
    "ObligationEvent",
    "ObligationEventKind",
    "Plan",
    "PlanStatus",
    "CompletionCheck",
    # Learning
    "LearningRule",
    "RuleFiring",
    "CorrectionCandidate",
    "CorrectionCandidateStatus",
    # Delegations
    "Delegation",
    "DelegationStatus",
    # Run / Incidents / Actions
    "RunLog",
    "Incident",
    "IncidentSeverity",
    "IncidentStatus",
    "ActionLog",
    "ActionClass",
    "ActionOutcome",
    # Quality
    "QualityAudit",
    "QualityScore",
    # Mesh
    "IntercomMessage",
    "IntercomAck",
    "IntercomState",
    # Sessions + Metrics
    "Session",
    "Metric",
    # Content creation
    "Exemplar",
    "Iteration",
    "IterationStatus",
    "Template",
    "Calibration",
    # OpenBrain (vector column added in Sprint 7)
    "Thought",
    "ThoughtSource",
    "IngestionRun",
    # Renderer
    "RenderResult",
    "VaultRenderer",
    "obligation_filename",
    "render_obligation_md",
    "render_learning_rules_md",
    "slugify",
    # Watcher
    "ApplyResult",
    "ParsedObligation",
    "VaultWatcher",
    "apply_modified",
    "apply_moved",
    "column_status",
    "is_rendered_obligation_path",
    "parse_obligation_md",
    # Helpers
    "new_id",
    "utcnow",
]
