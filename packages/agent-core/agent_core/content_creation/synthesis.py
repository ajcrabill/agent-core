"""Synthetic edge-case battery — L21.

Threshold-gated, post-onboarding feature: once a content-creation skill has
accumulated enough natural training data, the agent offers to generate
synthetic raw-input cases (covering edge cases that haven't shown up
naturally yet) so the user can iterate through them and accelerate training
by weeks.

Per L21 (locked):
  - **Threshold-gated**: never available at fresh setup
  - **Always optional**: never auto-runs; user must explicitly start a batch
  - **Tagged distinctly**: synthetic iterations + exemplars carry is_synthetic
    so calibration can detect overfit-to-synthetic

Defaults match the design doc:
  min_natural_exemplars: 15
  min_days_of_data: 7
  min_correction_types: 3  (encourages diverse correction patterns
                             before generating)

The actual generation is delegated to a BatteryGenerator Protocol — real
implementations call a strong model with the existing exemplars + corrections
as input; tests use a stub.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sqlmodel import select

from agent_core.content_creation.exemplars import ExemplarStore
from agent_core.content_creation.iterations import Iterations
from agent_core.state.db import Database
from agent_core.state.models import Exemplar, Iteration, IterationStatus, utcnow

logger = logging.getLogger(__name__)


# ── Eligibility ──────────────────────────────────────────────────────────────


@dataclass
class BatteryEligibility:
    """Whether the skill is ready for a synthetic-battery batch."""

    eligible: bool
    skill: str
    reason: str
    natural_exemplar_count: int = 0
    synthetic_exemplar_count: int = 0
    days_of_data: int = 0
    distinct_correction_themes: int = 0
    requirements: dict[str, int | float] = field(default_factory=dict)

    def as_summary(self) -> str:
        if self.eligible:
            return (
                f"Skill `{self.skill}` is eligible: "
                f"{self.natural_exemplar_count} natural exemplars, "
                f"{self.days_of_data}d of data, "
                f"{self.distinct_correction_themes} correction themes."
            )
        return f"Skill `{self.skill}` not eligible: {self.reason}"


# ── Generator Protocol ───────────────────────────────────────────────────────


@runtime_checkable
class BatteryGenerator(Protocol):
    """Produce synthetic raw inputs that target underrepresented patterns.

    Real implementations call a strong model (Opus / v4-pro tier) with the
    existing exemplars + corrections as input. Tests use a stub returning
    fixed text.
    """

    def generate(
        self,
        *,
        skill: str,
        exemplars: list[Exemplar],
        recent_corrections: list[str],
        count: int,
    ) -> list[str]: ...


# ── SyntheticBattery orchestrator ────────────────────────────────────────────


class SyntheticBattery:
    """Eligibility check + batch generation for the L21 synthetic battery.

    The orchestrator is dependency-injected with an ExemplarStore and an
    Iterations API so the same machinery handles natural and synthetic
    iterations identically — synthetic items just flow through the same
    Iterations pipeline with `is_synthetic=True`.

    Generation itself is delegated to an injected BatteryGenerator Protocol
    (Hermes-backed in prod; stub in tests).
    """

    def __init__(
        self,
        db: Database,
        *,
        exemplar_store: ExemplarStore | None = None,
        iterations: Iterations | None = None,
        min_natural_exemplars: int = 15,
        min_days_of_data: int = 7,
        min_correction_themes: int = 3,
    ) -> None:
        self.db = db
        self.exemplars = exemplar_store or ExemplarStore(db)
        self.iterations = iterations or Iterations(db, exemplar_store=self.exemplars)
        self.min_natural_exemplars = min_natural_exemplars
        self.min_days_of_data = min_days_of_data
        self.min_correction_themes = min_correction_themes

    @classmethod
    def from_settings(
        cls,
        settings: object,
        db: "Database",
        *,
        exemplar_store: "ExemplarStore | None" = None,
        iterations: "Iterations | None" = None,
    ) -> "SyntheticBattery":
        """Build from ``AgentSettings``: reads ``settings.learning.synthetic_min_*``."""
        lc = settings.learning  # type: ignore[attr-defined]
        return cls(
            db,
            exemplar_store=exemplar_store,
            iterations=iterations,
            min_natural_exemplars=lc.synthetic_min_natural_exemplars,
            min_days_of_data=lc.synthetic_min_days_of_data,
            min_correction_themes=lc.synthetic_min_correction_themes,
        )

    # ── Eligibility ────────────────────────────────────────────────────────

    def check_eligibility(self, skill: str) -> BatteryEligibility:
        """Return a structured eligibility report for ``skill``.

        Fails closed — any unmet requirement → eligible=False with a
        clear reason.
        """
        natural = self.exemplars.count_natural(skill)
        synthetic = self.exemplars.count_synthetic(skill)
        requirements = {
            "min_natural_exemplars": self.min_natural_exemplars,
            "min_days_of_data": self.min_days_of_data,
            "min_correction_themes": self.min_correction_themes,
        }

        if natural < self.min_natural_exemplars:
            return BatteryEligibility(
                eligible=False,
                skill=skill,
                reason=(f"only {natural} natural exemplars (need {self.min_natural_exemplars})"),
                natural_exemplar_count=natural,
                synthetic_exemplar_count=synthetic,
                requirements=requirements,
            )

        # Days of data: oldest natural exemplar to now
        days = _days_of_natural_data(self.db, skill)
        if days < self.min_days_of_data:
            return BatteryEligibility(
                eligible=False,
                skill=skill,
                reason=(f"only {days}d of natural data (need {self.min_days_of_data})"),
                natural_exemplar_count=natural,
                synthetic_exemplar_count=synthetic,
                days_of_data=days,
                requirements=requirements,
            )

        themes = _distinct_correction_themes(self.db, skill)
        if themes < self.min_correction_themes:
            return BatteryEligibility(
                eligible=False,
                skill=skill,
                reason=(
                    f"only {themes} distinct correction themes (need {self.min_correction_themes})"
                ),
                natural_exemplar_count=natural,
                synthetic_exemplar_count=synthetic,
                days_of_data=days,
                distinct_correction_themes=themes,
                requirements=requirements,
            )

        return BatteryEligibility(
            eligible=True,
            skill=skill,
            reason="all requirements met",
            natural_exemplar_count=natural,
            synthetic_exemplar_count=synthetic,
            days_of_data=days,
            distinct_correction_themes=themes,
            requirements=requirements,
        )

    # ── Batch generation ───────────────────────────────────────────────────

    def generate_batch(
        self,
        *,
        skill: str,
        count: int,
        generator: BatteryGenerator,
    ) -> list[Iteration]:
        """Run an eligibility check, call the generator, create N synthetic
        iterations.

        Raises ValueError if not eligible — call check_eligibility first
        and surface the reason in your UX.

        The user iterates through the resulting items the same way they
        iterate natural ones (via Iterations.add_attempt → add_correction →
        ratify). Each ratified item becomes a synthetic exemplar.
        """
        elig = self.check_eligibility(skill)
        if not elig.eligible:
            raise ValueError(f"skill {skill!r} not eligible: {elig.reason}")

        exemplars = self.exemplars.get_by_skill(skill, include_synthetic=False)
        recent_corrections = _recent_corrections(self.db, skill)

        raw_inputs = generator.generate(
            skill=skill,
            exemplars=exemplars,
            recent_corrections=recent_corrections,
            count=count,
        )

        out: list[Iteration] = []
        for raw in raw_inputs:
            it = self.iterations.start(
                skill=skill,
                raw_input=raw,
                is_synthetic=True,
            )
            out.append(it)
        logger.info(
            "synthetic battery generated: skill=%s, %d iterations",
            skill,
            len(out),
        )
        return out

    # ── Overfit detection ──────────────────────────────────────────────────

    def audit_overfit(self, skill: str) -> dict[str, float | int]:
        """Compare ratification rates on natural vs synthetic iterations.

        If synthetic ratification rate is much higher than natural, the
        agent is potentially overfitting to the synthetic distribution.
        """
        with self.db.session() as s:
            iters = list(s.exec(select(Iteration).where(Iteration.skill == skill)).all())

        natural = [i for i in iters if not i.is_synthetic]
        synthetic = [i for i in iters if i.is_synthetic]

        def rate(items: list[Iteration]) -> float:
            if not items:
                return 0.0
            ratified = sum(1 for i in items if i.status == IterationStatus.ratified)
            return ratified / len(items)

        nat_rate = rate(natural)
        syn_rate = rate(synthetic)
        return {
            "natural_iterations": len(natural),
            "synthetic_iterations": len(synthetic),
            "natural_ratification_rate": nat_rate,
            "synthetic_ratification_rate": syn_rate,
            # Positive = synthetic overperforming (overfit risk)
            "delta": syn_rate - nat_rate,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _days_of_natural_data(db: Database, skill: str) -> int:
    """Span (in days) from the oldest natural exemplar to now."""
    with db.session() as s:
        oldest = s.exec(
            select(Exemplar)
            .where(Exemplar.skill == skill)
            .where(Exemplar.is_synthetic == False)  # noqa: E712
            .order_by(Exemplar.created_at.asc())
        ).first()
    if oldest is None:
        return 0
    created = oldest.created_at
    if created.tzinfo is None:
        from datetime import UTC

        created = created.replace(tzinfo=UTC)
    return (utcnow() - created).days


def _distinct_correction_themes(db: Database, skill: str) -> int:
    """Crude proxy for diversity of corrections seen so far.

    Uses the first non-stopword token of each correction's narrative as the
    theme key. Real implementations might cluster semantically; this is the
    cheap first cut.
    """
    with db.session() as s:
        iters = list(
            s.exec(
                select(Iteration)
                .where(Iteration.skill == skill)
                .where(Iteration.is_synthetic == False)  # noqa: E712
            ).all()
        )

    keys: Counter = Counter()
    for it in iters:
        for c in it.corrections or []:
            narrative = (c.get("narrative") or "").strip().lower()
            tokens = [t for t in narrative.split() if t.isalpha() and len(t) > 2]
            if tokens:
                keys[tokens[0]] += 1
    return len(keys)


def _recent_corrections(db: Database, skill: str, limit: int = 50) -> list[str]:
    """Most-recent correction narratives, for feeding into the generator."""
    with db.session() as s:
        iters = list(
            s.exec(
                select(Iteration)
                .where(Iteration.skill == skill)
                .order_by(Iteration.created_at.desc())
            ).all()
        )
    out: list[str] = []
    for it in iters:
        for c in it.corrections or []:
            n = c.get("narrative")
            if n:
                out.append(n)
            if len(out) >= limit:
                return out
    return out


__all__ = [
    "BatteryEligibility",
    "BatteryGenerator",
    "SyntheticBattery",
]
