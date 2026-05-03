"""agent_core.state — canonical operational state.

This module owns the database schema, dual-backend (sqlite/postgres) connection
management, schema migrations, and the markdown<->db projection layer.

The database is the source of truth. The vault is a generated projection.
"""

from agent_core.state.models import (
    ActionClass,
    ActionLog,
    ActionOutcome,
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

__all__ = [
    # Identity
    "Identity",
    "Peer",
    "PeerRole",
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
    # Helpers
    "new_id",
    "utcnow",
]
