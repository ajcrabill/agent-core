"""agent_core.quality — two-tier quality auditor + auto-undelegation.

Per locked decision L8: always-on in both products. The bias-for-action
design (L9) makes a working quality auditor non-negotiable — autonomous
agents need a structural backstop that catches regression before users do.

What this layer does:
  - Audits delivered work via a stronger model (the "primary" auditor)
  - Optionally meta-audits the primary auditor with a stronger-still model
  - Tracks per-(subject_model, task_type) running scores
  - Auto-undelegates a (model, task_type) combo when scores drop below
    threshold for N consecutive audits — agent loop / skill dispatcher
    consults `is_delegated()` before assigning new work to that combo
  - Weekly report aggregates the audit log and highlights drift

Lifted from Esby's Admin/skills/quality-auditor/ pattern, generalized.
"""

from agent_core.quality.auditor import QualityAuditor
from agent_core.quality.protocols import AuditorModel, AuditScore
from agent_core.quality.report import (
    TaskTypeStats,
    WeeklyReport,
    generate_weekly_report,
)
from agent_core.quality.sampling import SamplingPolicy

__all__ = [
    "AuditScore",
    "AuditorModel",
    "QualityAuditor",
    "SamplingPolicy",
    "TaskTypeStats",
    "WeeklyReport",
    "generate_weekly_report",
]
