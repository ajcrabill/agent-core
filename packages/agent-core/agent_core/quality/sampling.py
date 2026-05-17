"""Sampling policy — decide which work items to audit.

Auditing every closure is expensive. Esby's pattern (which we lift):
  - Random sample at a base rate (default 10%)
  - Always sample low-confidence outputs (below threshold)
  - Always sample the first N audits of a brand-new (model, task_type)
    combo to bootstrap the score

This is a small standalone helper; the auditor itself just consults it.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import QualityScore


class SamplingPolicy:
    """Decide whether to audit a given (task_type, task_id, confidence).

    Tunables:
      base_rate: fraction of work sampled at random (default 0.1)
      low_confidence_threshold: always audit below this confidence (default 0.7)
      bootstrap_count: always audit the first N of any new (model, task_type)
    """

    def __init__(
        self,
        db: Database,
        *,
        base_rate: float = 0.1,
        low_confidence_threshold: float = 0.7,
        bootstrap_count: int = 5,
        rng: Callable[[], float] | None = None,
    ) -> None:
        if not 0.0 <= base_rate <= 1.0:
            raise ValueError("base_rate must be in [0, 1]")
        self.db = db
        self.base_rate = base_rate
        self.low_confidence_threshold = low_confidence_threshold
        self.bootstrap_count = bootstrap_count
        self._rng = rng or random.random

    @classmethod
    def from_settings(
        cls,
        settings: object,
        db: Database,
        *,
        bootstrap_count: int = 5,
        rng: Callable[[], float] | None = None,
    ) -> SamplingPolicy:
        """Build from ``AgentSettings``: reads ``settings.quality.audit_sample_rate``
        and ``settings.quality.low_confidence_audit_threshold``."""
        q = settings.quality  # type: ignore[attr-defined]
        return cls(
            db,
            base_rate=q.audit_sample_rate,
            low_confidence_threshold=q.low_confidence_audit_threshold,
            bootstrap_count=bootstrap_count,
            rng=rng,
        )

    def should_audit(
        self,
        *,
        task_type: str,
        subject_model: str,
        confidence: float = 1.0,
    ) -> tuple[bool, str]:
        """Returns (should_audit, reason).

        Reason is a short string captured into QualityAudit.sampling_reason
        so weekly reports can show why coverage looked the way it did.
        """
        if confidence < self.low_confidence_threshold:
            return True, "low_confidence"

        # Bootstrap: the first `bootstrap_count` audits per (model, task_type)
        with self.db.session() as s:
            score_row = s.exec(
                select(QualityScore)
                .where(QualityScore.subject_model == subject_model)
                .where(QualityScore.task_type == task_type)
            ).first()
        existing = score_row.total_audited if score_row else 0
        if existing < self.bootstrap_count:
            return True, "bootstrap"

        if self._rng() < self.base_rate:
            return True, "random"

        return False, "skipped_random"


__all__ = ["SamplingPolicy"]
