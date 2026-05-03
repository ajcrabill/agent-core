"""agent_core.learning — supervised-learning store + correction flow.

The L12 + L13 + L15 substrate. The chat-driven capture UX, weekly review,
pre-seed packs, and maintenance scan land in Sprint 5b on top of this layer.

Modules in this sprint:
  store.py       — LearningStore (CRUD + JSONL write-ahead + supersede chain)
  firings.py     — record + query when rules actually fire in context
  candidates.py  — auto-detected correction-candidates flow (propose, promote, reject)

The schema (LearningRule, RuleFiring, CorrectionCandidate) lives in
agent_core.state.models. This package wraps it with the operational APIs.
"""

from agent_core.learning.candidates import CorrectionCandidates
from agent_core.learning.detector import (
    CorrectionDetector,
    DetectedCorrection,
    HeuristicDetector,
)
from agent_core.learning.firings import RuleFirings
from agent_core.learning.maintenance import (
    CompactableCluster,
    ConflictFinding,
    DuplicateFinding,
    MaintenanceReport,
    MaintenanceScan,
)
from agent_core.learning.review import (
    WeeklyLearningReview,
    WeeklyLearningReviewBuilder,
)
from agent_core.learning.seed_packs import list_packs, load_pack, pack_metadata
from agent_core.learning.store import LearningStore

__all__ = [
    "CompactableCluster",
    "ConflictFinding",
    "CorrectionCandidates",
    "CorrectionDetector",
    "DetectedCorrection",
    "DuplicateFinding",
    "HeuristicDetector",
    "LearningStore",
    "MaintenanceReport",
    "MaintenanceScan",
    "RuleFirings",
    "WeeklyLearningReview",
    "WeeklyLearningReviewBuilder",
    "list_packs",
    "load_pack",
    "pack_metadata",
]
