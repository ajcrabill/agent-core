"""CalibrationManager — per-skill confidence + autonomous-mode gate.

The skill starts in **review mode** (every output goes to human review). After
``ratifications_required`` consecutive ratifications above the confidence
threshold, the skill flips to **autonomous mode** (output delivered directly,
sampled by quality auditor for safety).

Resets after a meaningful correction — the user's correction signals the
agent's calibration was off; we go back to review mode until the streak
re-establishes.
"""

from __future__ import annotations

import logging

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import Calibration, utcnow

logger = logging.getLogger(__name__)


class CalibrationManager:
    """Per-skill autonomous-mode calibration.

    Tunables:
      default_threshold: confidence required to flip autonomous_mode True
                         (default 0.85; per-skill override on the row)
      ratifications_required: consecutive ratifications needed
                              (default 5 — Esby's working baseline)
    """

    def __init__(
        self,
        db: Database,
        *,
        default_threshold: float = 0.85,
        ratifications_required: int = 5,
    ) -> None:
        self.db = db
        self.default_threshold = default_threshold
        self.ratifications_required = ratifications_required

    # ── Read API ────────────────────────────────────────────────────────────

    def get(self, skill: str) -> Calibration:
        """Get-or-create the Calibration row for ``skill``."""
        with self.db.session() as s:
            row = s.exec(select(Calibration).where(Calibration.skill == skill)).first()
            if row is None:
                row = Calibration(
                    skill=skill,
                    confidence=0.0,
                    attempts_count=0,
                    ratifications_count=0,
                    consecutive_ratifications=0,
                    autonomous_mode=False,
                    autonomous_mode_threshold=self.default_threshold,
                )
                s.add(row)
                s.commit()
                s.refresh(row)
            return row

    def is_autonomous(self, skill: str) -> bool:
        return self.get(skill).autonomous_mode

    # ── Mutators ────────────────────────────────────────────────────────────

    def record_attempt(
        self,
        skill: str,
        *,
        ratified: bool,
        confidence: float | None = None,
    ) -> Calibration:
        """Bump the counters for one iteration outcome.

        ``ratified``=True means the user accepted the agent's output (possibly
        after edits — that's still ratified). False means the user abandoned
        or overrode meaningfully — resets consecutive_ratifications.

        ``confidence`` (optional) updates the running confidence with this
        attempt's score (typically from quality auditor).
        """
        with self.db.session() as s:
            row = s.exec(select(Calibration).where(Calibration.skill == skill)).first()
            if row is None:
                row = Calibration(
                    skill=skill,
                    confidence=0.0,
                    autonomous_mode_threshold=self.default_threshold,
                )
                s.add(row)
                s.flush()

            row.attempts_count += 1
            if ratified:
                row.ratifications_count += 1
                row.consecutive_ratifications += 1
            else:
                # Meaningful failure resets the streak. Quality auditor's
                # auto-undelegation handles the model side; this handles the
                # autonomous-mode gate per skill.
                row.consecutive_ratifications = 0
                row.autonomous_mode = False  # demote on a single fail

            if confidence is not None:
                row.confidence = confidence

            # Promote to autonomous mode if threshold met
            should_autonomize = (
                not row.autonomous_mode
                and row.confidence >= row.autonomous_mode_threshold
                and row.consecutive_ratifications >= self.ratifications_required
            )
            if should_autonomize:
                row.autonomous_mode = True
                logger.info(
                    "skill %s promoted to autonomous mode (confidence=%.2f, streak=%d)",
                    skill,
                    row.confidence,
                    row.consecutive_ratifications,
                )

            row.last_calibrated_at = utcnow()
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def reset(self, skill: str) -> Calibration:
        """Reset a skill's calibration entirely. Use after a major correction
        or model swap that invalidates prior history."""
        with self.db.session() as s:
            row = s.exec(select(Calibration).where(Calibration.skill == skill)).first()
            if row is None:
                return self.get(skill)
            row.confidence = 0.0
            row.consecutive_ratifications = 0
            row.autonomous_mode = False
            row.last_calibrated_at = utcnow()
            s.add(row)
            s.commit()
            s.refresh(row)
            return row


__all__ = ["CalibrationManager"]
