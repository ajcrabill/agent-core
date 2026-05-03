"""agent_core.state — canonical operational state.

This module owns the database schema, dual-backend (sqlite/postgres) connection
management, schema migrations, and the markdown<->db projection layer.

The database is the source of truth. The vault is a generated projection.
"""

from agent_core.state.models import (
    # Identity
    Identity,
    Peer,
    PeerRole,
    # Work
    Obligation,
    ObligationStatus,
    ObligationOwner,
    ObligationSource,
    ObligationEvent,
    ObligationEventKind,
    Plan,
    PlanStatus,
    CompletionCheck,
    # Learning
    LearningRule,
    RuleFiring,
    CorrectionCandidate,
    CorrectionCandidateStatus,
    # Helpers
    new_id,
    utcnow,
)

__all__ = [
    "Identity",
    "Peer",
    "PeerRole",
    "Obligation",
    "ObligationStatus",
    "ObligationOwner",
    "ObligationSource",
    "ObligationEvent",
    "ObligationEventKind",
    "Plan",
    "PlanStatus",
    "CompletionCheck",
    "LearningRule",
    "RuleFiring",
    "CorrectionCandidate",
    "CorrectionCandidateStatus",
    "new_id",
    "utcnow",
]
