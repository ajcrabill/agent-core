"""RuleFirings — record + query when learning rules actually fire.

Powers the "firing visibility" UX (Sprint 5b): the user sees which rules
actually matter and which haven't been used in a while (candidates for
removal in the weekly review).

A "firing" happens when:
  - The context loader includes a rule in the prompt envelope, AND
  - The rule was tagged for the current scope (general or skill-matched)

The context loader (Sprint 2) doesn't write firings itself — it would do so
on every collect() call, which is too noisy. Instead, the agent loop or
skill dispatcher decides when a rule "really" fired (e.g., once per
obligation per skill invocation) and records it explicitly.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import LearningRule, RuleFiring, utcnow

logger = logging.getLogger(__name__)


class RuleFirings:
    """Append-only firing log + read helpers."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Record ──────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        rule_id: str,
        skill: str | None = None,
        obligation_id: str | None = None,
        action_summary: str | None = None,
        was_overridden: bool = False,
    ) -> RuleFiring:
        """Log that a rule was applied to this work. ``was_overridden=True``
        means the agent applied the rule and the user reverted it (or the
        agent self-corrected) — useful for spotting rules under contention."""
        firing = RuleFiring(
            rule_id=rule_id,
            skill=skill,
            obligation_id=obligation_id,
            action_summary=action_summary,
            was_overridden=was_overridden,
        )
        with self.db.session() as s:
            s.add(firing)
            s.commit()
            s.refresh(firing)
        return firing

    # ── Query ───────────────────────────────────────────────────────────────

    def for_rule(self, rule_id: str, *, limit: int | None = None) -> list[RuleFiring]:
        with self.db.session() as s:
            stmt = (
                select(RuleFiring)
                .where(RuleFiring.rule_id == rule_id)
                .order_by(RuleFiring.fired_at.desc())
            )
            rows = list(s.exec(stmt).all())
        return rows[:limit] if limit else rows

    def count_for_rule(self, rule_id: str) -> int:
        with self.db.session() as s:
            return len(list(s.exec(select(RuleFiring).where(RuleFiring.rule_id == rule_id)).all()))

    def overrides_for_rule(self, rule_id: str) -> int:
        """How many times the agent applied this rule and was overridden.
        High values indicate a stale or wrong rule that needs supersession."""
        with self.db.session() as s:
            return len(
                list(
                    s.exec(
                        select(RuleFiring)
                        .where(RuleFiring.rule_id == rule_id)
                        .where(RuleFiring.was_overridden == True)  # noqa: E712
                    ).all()
                )
            )

    def stale_rules(self, *, days: int = 90) -> list[LearningRule]:
        """Active rules that haven't fired in N days. Candidates for removal
        in the weekly review surface."""
        cutoff = utcnow() - timedelta(days=days)
        with self.db.session() as s:
            active_rules = list(
                s.exec(select(LearningRule).where(LearningRule.superseded_by.is_(None))).all()
            )

        out: list[LearningRule] = []
        for rule in active_rules:
            with self.db.session() as s:
                last = s.exec(
                    select(RuleFiring)
                    .where(RuleFiring.rule_id == rule.id)
                    .order_by(RuleFiring.fired_at.desc())
                ).first()
            if last is None:
                # Never fired — also stale
                out.append(rule)
            elif _aware(last.fired_at) < cutoff:
                out.append(rule)
        return out


def _aware(dt):  # type: ignore[no-untyped-def]
    """Make a datetime timezone-aware (assume UTC if naïve).

    SQLite stores datetimes naïve; this normalizes for comparison against
    utcnow() (which is timezone-aware).
    """
    if dt is None:
        return dt
    if dt.tzinfo is None:
        from datetime import UTC

        return dt.replace(tzinfo=UTC)
    return dt


__all__ = ["RuleFirings"]
