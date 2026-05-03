"""CorrectionCandidates — auto-detected corrections awaiting promotion.

The capture detector (Sprint 5b, LLM-driven) writes CorrectionCandidate rows
when it spots the principal correcting the agent in chat. The user reviews
each one in the weekly-review surface and either:

  - **Promotes** it to a real LearningRule (one-click in the UX)
  - **Rejects** it (false positive — the detector misread the chat)

This module is the API the UX (and tests) call.
"""

from __future__ import annotations

import logging

from sqlmodel import select

from agent_core.learning.store import LearningStore
from agent_core.state.db import Database
from agent_core.state.models import (
    CorrectionCandidate,
    CorrectionCandidateStatus,
    LearningRule,
)

logger = logging.getLogger(__name__)


class CorrectionCandidates:
    """API for the candidate flow."""

    def __init__(self, db: Database, store: LearningStore | None = None) -> None:
        self.db = db
        self.store = store or LearningStore(db, write_ahead=False)

    # ── Detector calls these to surface candidates ──────────────────────────

    def propose(
        self,
        *,
        detected_correction: str,
        inferred_skill_tags: list[str] | None = None,
        source_session: str | None = None,
        source_excerpt: str | None = None,
        confidence: float = 0.0,
    ) -> CorrectionCandidate:
        """Detector found a correction; surface for human review."""
        cc = CorrectionCandidate(
            detected_correction=detected_correction,
            inferred_skill_tags=inferred_skill_tags or ["general"],
            source_session=source_session,
            source_excerpt=source_excerpt,
            confidence=confidence,
            status=CorrectionCandidateStatus.pending,
        )
        with self.db.session() as s:
            s.add(cc)
            s.commit()
            s.refresh(cc)
        logger.info(
            "correction candidate proposed: id=%s confidence=%.2f",
            cc.id[:8],
            confidence,
        )
        return cc

    # ── Queries for the review UX ───────────────────────────────────────────

    def pending(self) -> list[CorrectionCandidate]:
        with self.db.session() as s:
            return list(
                s.exec(
                    select(CorrectionCandidate)
                    .where(CorrectionCandidate.status == CorrectionCandidateStatus.pending)
                    .order_by(CorrectionCandidate.created_at.desc())
                ).all()
            )

    def get(self, candidate_id: str) -> CorrectionCandidate | None:
        with self.db.session() as s:
            return s.get(CorrectionCandidate, candidate_id)

    # ── User actions from the review UX ─────────────────────────────────────

    def promote(
        self,
        candidate_id: str,
        *,
        edited_correction: str | None = None,
        skill_tags: list[str] | None = None,
        notes: str = "",
    ) -> LearningRule:
        """Promote a candidate to a real LearningRule.

        ``edited_correction`` lets the user edit the detector's text before
        promoting. ``skill_tags`` overrides the inferred tags. The candidate
        is marked ``promoted`` and points at the new rule via
        ``promoted_to_rule_id``.
        """
        with self.db.session() as s:
            cc = s.get(CorrectionCandidate, candidate_id)
            if cc is None:
                raise ValueError(f"candidate {candidate_id!r} not found")
            if cc.status != CorrectionCandidateStatus.pending:
                raise ValueError(f"candidate {candidate_id!r} already {cc.status.value}")

        rule = self.store.add(
            correction=edited_correction or cc.detected_correction,
            skill_tags=skill_tags if skill_tags is not None else list(cc.inferred_skill_tags or []),
            source=f"correction-candidate:{cc.id[:8]}"
            + (f" session:{cc.source_session}" if cc.source_session else ""),
            context=cc.source_excerpt or "",
            notes=notes,
        )

        with self.db.session() as s:
            cc = s.get(CorrectionCandidate, candidate_id)
            cc.status = CorrectionCandidateStatus.promoted
            cc.promoted_to_rule_id = rule.id
            s.add(cc)
            s.commit()

        logger.info(
            "correction candidate promoted: %s → rule %s",
            candidate_id[:8],
            rule.id[:8],
        )
        return rule

    def reject(self, candidate_id: str) -> CorrectionCandidate:
        """Mark a candidate as a false positive.

        TODO: when CorrectionCandidate gets a notes column (future migration),
        accept a `reason` arg and capture it for posterity.
        """
        return self._transition(
            candidate_id,
            new_status=CorrectionCandidateStatus.rejected,
        )

    def expire(self, candidate_id: str) -> CorrectionCandidate:
        """Mark a candidate as expired (e.g., the weekly review batch
        passed without action)."""
        return self._transition(
            candidate_id,
            new_status=CorrectionCandidateStatus.expired,
        )

    # ── Internals ───────────────────────────────────────────────────────────

    def _transition(
        self,
        candidate_id: str,
        *,
        new_status: CorrectionCandidateStatus,
    ) -> CorrectionCandidate:
        with self.db.session() as s:
            cc = s.get(CorrectionCandidate, candidate_id)
            if cc is None:
                raise ValueError(f"candidate {candidate_id!r} not found")
            if cc.status != CorrectionCandidateStatus.pending:
                raise ValueError(f"candidate {candidate_id!r} already {cc.status.value}")
            cc.status = new_status
            s.add(cc)
            s.commit()
            s.refresh(cc)
        return cc


__all__ = ["CorrectionCandidates"]
