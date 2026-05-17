"""QualityAuditor orchestrator.

Wraps an injected AuditorModel, persists results to the QualityAudit table,
maintains running per-(subject_model, task_type) stats in QualityScore, and
auto-undelegates when scores drop below threshold for N consecutive audits.

Two-tier audit:
  primary auditor: the cheaper-but-still-strong model that scores subject work
  meta auditor (optional): the stronger model that spot-checks the primary
    (so the auditor itself can't silently regress). When provided, every Nth
    primary audit is meta-audited.

Auto-undelegation policy:
  - If the most recent `undelegation_strikes` audits for a (subject_model,
    task_type) pair all failed, set is_delegated=False.
  - Agent loop / skill dispatcher checks `is_delegated(model, task_type)`
    before sending new work to that combo.
  - `restore_delegation()` flips it back manually after a fix.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlmodel import select

from agent_core.quality.protocols import AuditorModel
from agent_core.state.db import Database
from agent_core.state.models import (
    QualityAudit,
    QualityScore,
    utcnow,
)

logger = logging.getLogger(__name__)


class QualityAuditor:
    """Run audits, track scores, manage delegation.

    Tunables (sensible defaults match Esby's working setup):
      pass_threshold: 0.6 — scores at/above this are 'passed'
      undelegation_strikes: 3 — consecutive failures triggering undelegation
      last_n_window: 10 — running window for `last_n_avg` stat
    """

    def __init__(
        self,
        db: Database,
        primary_auditor: AuditorModel,
        *,
        meta_auditor: AuditorModel | None = None,
        meta_audit_every_n: int = 10,
        pass_threshold: float = 0.6,
        undelegation_strikes: int = 3,
        last_n_window: int = 10,
        primary_auditor_model_name: str = "primary-auditor",
        meta_auditor_model_name: str = "meta-auditor",
    ) -> None:
        self.db = db
        self.primary_auditor = primary_auditor
        self.meta_auditor = meta_auditor
        self.meta_audit_every_n = meta_audit_every_n
        self.pass_threshold = pass_threshold
        self.undelegation_strikes = undelegation_strikes
        self.last_n_window = last_n_window
        self.primary_auditor_model_name = primary_auditor_model_name
        self.meta_auditor_model_name = meta_auditor_model_name

    @classmethod
    def from_settings(
        cls,
        settings: object,
        db: Database,
        primary_auditor: AuditorModel,
        *,
        meta_auditor: AuditorModel | None = None,
        meta_audit_every_n: int = 10,
        primary_auditor_model_name: str = "primary-auditor",
        meta_auditor_model_name: str = "meta-auditor",
    ) -> QualityAuditor:
        """Build from ``AgentSettings``: reads ``settings.quality.*`` and
        ``settings.autonomy.auto_undelegate_after_n_failures``."""
        q = settings.quality  # type: ignore[attr-defined]
        a = settings.autonomy  # type: ignore[attr-defined]
        return cls(
            db,
            primary_auditor,
            meta_auditor=meta_auditor,
            meta_audit_every_n=meta_audit_every_n,
            pass_threshold=q.pass_threshold,
            undelegation_strikes=a.auto_undelegate_after_n_failures,
            last_n_window=q.last_n_window,
            primary_auditor_model_name=primary_auditor_model_name,
            meta_auditor_model_name=meta_auditor_model_name,
        )

    # ── Run audits ──────────────────────────────────────────────────────────

    def audit(
        self,
        *,
        task_type: str,
        task_id: str,
        subject_model: str,
        output_summary: str,
        sampling_reason: str = "random",
        rubrics: list[str] | None = None,
    ) -> QualityAudit:
        """Run a level-1 (primary) audit. Persists result + updates running
        score. Returns the persisted QualityAudit row.

        Optionally triggers a level-2 meta-audit on every Nth primary audit
        (if meta_auditor is configured)."""
        score = self.primary_auditor.audit(
            task_type=task_type,
            subject_model=subject_model,
            output_summary=output_summary,
            rubrics=rubrics,
        )
        # Use orchestrator's threshold for the official pass/fail (the model's
        # own `passed` is captured but not authoritative).
        passed = score.score >= self.pass_threshold

        with self.db.session() as s:
            audit_row = QualityAudit(
                audit_level=1,
                auditor_model=self.primary_auditor_model_name,
                subject_model=subject_model,
                task_type=task_type,
                task_id=task_id,
                score=score.score,
                passed=passed,
                primary_notes=score.primary_notes,
                sampling_reason=sampling_reason,
            )
            s.add(audit_row)
            s.commit()
            s.refresh(audit_row)

        # Update running stats (separate session — keeps the audit row durable
        # even if score-update raises)
        self._update_score(subject_model, task_type, audit_row.score, passed)

        # Optionally meta-audit
        if self.meta_auditor is not None:
            self._maybe_meta_audit(audit_row, output_summary, rubrics)

        return audit_row

    def _maybe_meta_audit(
        self,
        primary_audit: QualityAudit,
        output_summary: str,
        rubrics: list[str] | None,
    ) -> None:
        """Run a meta-audit on every Nth primary audit, scoping by task_type."""
        with self.db.session() as s:
            count = s.exec(
                select(QualityAudit)
                .where(QualityAudit.audit_level == 1)
                .where(QualityAudit.task_type == primary_audit.task_type)
            ).all()
        if len(count) % self.meta_audit_every_n != 0:
            return

        # Meta-audit: assess the primary auditor's score itself
        meta_score = self.meta_auditor.audit(  # type: ignore[union-attr]
            task_type=primary_audit.task_type,
            subject_model=self.primary_auditor_model_name,
            output_summary=(
                f"primary auditor scored {primary_audit.score:.2f} "
                f"({'passed' if primary_audit.passed else 'failed'}) "
                f"on {primary_audit.task_type} task. "
                f"Notes: {primary_audit.primary_notes or '(none)'} | "
                f"Original output summary: {output_summary}"
            ),
            rubrics=rubrics,
        )
        with self.db.session() as s:
            s.add(
                QualityAudit(
                    audit_level=2,
                    auditor_model=self.meta_auditor_model_name,
                    subject_model=self.primary_auditor_model_name,
                    task_type=primary_audit.task_type,
                    task_id=primary_audit.id,  # meta audit refers to the primary
                    score=meta_score.score,
                    passed=meta_score.score >= self.pass_threshold,
                    primary_notes=meta_score.primary_notes,
                    sampling_reason="meta_check",
                )
            )
            s.commit()

    # ── Score tracking ──────────────────────────────────────────────────────

    def _update_score(
        self,
        subject_model: str,
        task_type: str,
        new_score: float,
        passed: bool,
    ) -> None:
        with self.db.session() as s:
            row = s.exec(
                select(QualityScore)
                .where(QualityScore.subject_model == subject_model)
                .where(QualityScore.task_type == task_type)
            ).first()
            if row is None:
                row = QualityScore(
                    audit_level=1,
                    subject_model=subject_model,
                    task_type=task_type,
                    total_audited=0,
                    running_sum=0.0,
                    running_avg=0.0,
                    last_n_avg=None,
                    strikes=0,
                    is_delegated=True,
                )
                s.add(row)
                s.flush()  # assign id

            row.total_audited += 1
            row.running_sum += new_score
            row.running_avg = row.running_sum / row.total_audited
            row.last_audit_at = utcnow()

            # Recompute last-N average from recent audits
            recent = s.exec(
                select(QualityAudit)
                .where(QualityAudit.audit_level == 1)
                .where(QualityAudit.subject_model == subject_model)
                .where(QualityAudit.task_type == task_type)
                .order_by(QualityAudit.audited_at.desc())
            ).all()
            window = list(recent)[: self.last_n_window]
            if window:
                row.last_n_avg = sum(a.score for a in window) / len(window)

            # Update strikes / undelegation
            if passed:
                row.strikes = 0
            else:
                row.strikes += 1

            should_undelegate = row.is_delegated and row.strikes >= self.undelegation_strikes
            if should_undelegate:
                row.is_delegated = False
                row.last_undelegated_at = utcnow()
                logger.warning(
                    "auto-undelegating (%s, %s) after %d consecutive failures",
                    subject_model,
                    task_type,
                    row.strikes,
                )

            row.updated_at = utcnow()
            s.add(row)
            s.commit()

    # ── Public queries ──────────────────────────────────────────────────────

    def is_delegated(self, *, subject_model: str, task_type: str) -> bool:
        """Returns True if this (model, task_type) combo is currently
        delegated. Default True (no row yet → never been audited → trust)."""
        with self.db.session() as s:
            row = s.exec(
                select(QualityScore)
                .where(QualityScore.subject_model == subject_model)
                .where(QualityScore.task_type == task_type)
            ).first()
        return True if row is None else bool(row.is_delegated)

    def restore_delegation(self, *, subject_model: str, task_type: str) -> None:
        """Manually re-enable delegation (used after a fix). Resets strikes."""
        with self.db.session() as s:
            row = s.exec(
                select(QualityScore)
                .where(QualityScore.subject_model == subject_model)
                .where(QualityScore.task_type == task_type)
            ).first()
            if row is None:
                return
            row.is_delegated = True
            row.strikes = 0
            row.last_restored_at = utcnow()
            row.updated_at = utcnow()
            s.add(row)
            s.commit()
            logger.info("restored delegation for (%s, %s)", subject_model, task_type)

    def list_undelegated(self) -> Iterable[QualityScore]:
        with self.db.session() as s:
            return list(
                s.exec(
                    select(QualityScore).where(QualityScore.is_delegated == False)  # noqa: E712
                ).all()
            )


__all__ = ["QualityAuditor"]
