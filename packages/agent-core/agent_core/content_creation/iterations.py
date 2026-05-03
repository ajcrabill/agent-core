"""Iterations — manage the (raw → attempts → corrections → final) cycle.

This is where one piece of content actually gets refined. The agent
generates an attempt; the user marks it up; the agent re-generates; the
user finally ratifies. The ratified output gets promoted to an exemplar.

L21 fits cleanly: synthetic-battery iterations work the same way (raw input
came from the generator instead of a real input) but carry is_synthetic=True
through to the resulting exemplar so calibration can spot overfit.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlmodel import select

from agent_core.content_creation.calibration import CalibrationManager
from agent_core.content_creation.exemplars import ExemplarStore
from agent_core.state.db import Database
from agent_core.state.models import (
    Exemplar,
    Iteration,
    IterationStatus,
    utcnow,
)

logger = logging.getLogger(__name__)


class Iterations:
    """API for the raw → attempts → corrections → final cycle."""

    def __init__(
        self,
        db: Database,
        *,
        exemplar_store: ExemplarStore | None = None,
        calibration: CalibrationManager | None = None,
    ) -> None:
        self.db = db
        self.exemplars = exemplar_store or ExemplarStore(db)
        self.calibration = calibration or CalibrationManager(db)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(
        self,
        *,
        skill: str,
        raw_input: str,
        obligation_id: str | None = None,
        is_synthetic: bool = False,
    ) -> Iteration:
        """Begin a new iteration. The agent will produce attempts via
        ``add_attempt()`` until the user ratifies or abandons."""
        it = Iteration(
            skill=skill,
            raw_input=raw_input,
            obligation_id=obligation_id,
            is_synthetic=is_synthetic,
            status=IterationStatus.in_progress,
            attempts=[],
            corrections=[],
        )
        with self.db.session() as s:
            s.add(it)
            s.commit()
            s.refresh(it)
        logger.info("iteration started: skill=%s id=%s synth=%s", skill, it.id[:8], is_synthetic)
        return it

    def add_attempt(
        self,
        iteration_id: str,
        *,
        content: str,
        model: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Iteration:
        """Append the agent's attempt at producing the finished content.

        The attempt joins the ``attempts`` list with an index, content,
        model, timestamp, and any metadata.
        """
        with self.db.session() as s:
            it = s.get(Iteration, iteration_id)
            if it is None:
                raise ValueError(f"iteration {iteration_id!r} not found")
            if it.status != IterationStatus.in_progress:
                raise ValueError(
                    f"cannot add attempt to {it.status.value} iteration {iteration_id!r}"
                )
            attempts = list(it.attempts or [])
            attempts.append(
                {
                    "n": len(attempts),
                    "content": content,
                    "model": model,
                    "ts": utcnow().isoformat(),
                    **({"meta": meta} if meta else {}),
                }
            )
            it.attempts = attempts
            s.add(it)
            s.commit()
            s.refresh(it)
            return it

    def add_correction(
        self,
        iteration_id: str,
        *,
        narrative: str,
        diff: str | None = None,
    ) -> Iteration:
        """Record a user correction on the latest attempt.

        ``narrative`` is the user's verbal explanation ("change the tone to
        warmer"). ``diff`` is the optional structured edit (could be a
        unified diff, a JSON patch, or a side-by-side rewrite — the chat
        layer chooses the format).
        """
        with self.db.session() as s:
            it = s.get(Iteration, iteration_id)
            if it is None:
                raise ValueError(f"iteration {iteration_id!r} not found")
            if it.status != IterationStatus.in_progress:
                raise ValueError(f"cannot add correction to {it.status.value} iteration")
            corrections = list(it.corrections or [])
            corrections.append(
                {
                    "n": len(corrections),
                    "narrative": narrative,
                    "diff": diff,
                    "ts": utcnow().isoformat(),
                }
            )
            it.corrections = corrections
            s.add(it)
            s.commit()
            s.refresh(it)
            return it

    def ratify(
        self,
        iteration_id: str,
        *,
        final_content: str | None = None,
        exemplar_title: str | None = None,
        confidence: float | None = None,
    ) -> Exemplar:
        """User accepts this iteration; promote final content to an exemplar.

        ``final_content`` defaults to the last attempt. ``exemplar_title`` is
        optional human label. ``confidence`` (0-1) updates the calibration.
        Returns the freshly-created Exemplar.
        """
        with self.db.session() as s:
            it = s.get(Iteration, iteration_id)
            if it is None:
                raise ValueError(f"iteration {iteration_id!r} not found")
            if it.status != IterationStatus.in_progress:
                raise ValueError(f"iteration already {it.status.value}")
            chosen_final = final_content or _last_attempt_content(it)
            if not chosen_final:
                raise ValueError("no final_content provided and no attempts to default to")
            it.final_content = chosen_final
            it.status = IterationStatus.ratified
            it.ratified_at = utcnow()
            s.add(it)
            s.commit()
            s.refresh(it)

        # Promote to exemplar (carrying is_synthetic flag)
        ex = self.exemplars.add(
            skill=it.skill,
            content=chosen_final,
            title=exemplar_title,
            source_iteration_id=it.id,
            is_synthetic=it.is_synthetic,
            metadata={"obligation_id": it.obligation_id} if it.obligation_id else None,
        )

        # Update calibration: ratified=True
        self.calibration.record_attempt(it.skill, ratified=True, confidence=confidence)
        return ex

    def abandon(self, iteration_id: str, *, reason: str | None = None) -> Iteration:
        """User gives up on this iteration. Calibration counts it as a
        non-ratification (resets the streak)."""
        with self.db.session() as s:
            it = s.get(Iteration, iteration_id)
            if it is None:
                raise ValueError(f"iteration {iteration_id!r} not found")
            if it.status != IterationStatus.in_progress:
                raise ValueError(f"iteration already {it.status.value}")
            it.status = IterationStatus.abandoned
            if reason:
                # Stash the reason on the last correction-like entry for posterity
                corrections = list(it.corrections or [])
                corrections.append(
                    {
                        "n": len(corrections),
                        "narrative": f"[abandoned] {reason}",
                        "diff": None,
                        "ts": utcnow().isoformat(),
                    }
                )
                it.corrections = corrections
            s.add(it)
            s.commit()
            s.refresh(it)

        self.calibration.record_attempt(it.skill, ratified=False)
        return it

    # ── Queries ─────────────────────────────────────────────────────────────

    def get(self, iteration_id: str) -> Iteration | None:
        with self.db.session() as s:
            return s.get(Iteration, iteration_id)

    def in_progress(self, skill: str | None = None) -> list[Iteration]:
        with self.db.session() as s:
            stmt = select(Iteration).where(Iteration.status == IterationStatus.in_progress)
            if skill is not None:
                stmt = stmt.where(Iteration.skill == skill)
            return list(s.exec(stmt).all())


def _last_attempt_content(it: Iteration) -> str | None:
    if not it.attempts:
        return None
    return it.attempts[-1].get("content")


__all__ = ["Iterations"]
