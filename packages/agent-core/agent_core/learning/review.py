"""Weekly review surface — assemble everything the user needs to maintain
their learning rules.

Designed to be delivered via the daily-digest channel (email / chat / next-
session preamble). The user reads it once a week and one-clicks promotes /
rejects / supersedes.

Sections (all live):
  - Pending correction candidates from the past week
  - Newly-promoted rules from the past week (so user can sanity-check what
    they ratified; chance to supersede with a tweaked version)
  - Stale rules (haven't fired in N days) — candidates to retire
  - Possible duplicates / conflicts (from the maintenance scan)
  - Compactable clusters
  - Top-firing rules (the workhorses — confirmation that the system works)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import select

from agent_core.learning.candidates import CorrectionCandidates
from agent_core.learning.firings import RuleFirings
from agent_core.learning.maintenance import MaintenanceReport, MaintenanceScan
from agent_core.learning.store import LearningStore
from agent_core.state.db import Database
from agent_core.state.models import LearningRule, RuleFiring, utcnow


@dataclass
class WeeklyLearningReview:
    period_start: datetime
    period_end: datetime
    pending_candidates: list[dict] = field(default_factory=list)
    promoted_this_week: list[dict] = field(default_factory=list)
    maintenance: MaintenanceReport | None = None
    top_firing_rules: list[dict] = field(default_factory=list)

    def has_anything_to_review(self) -> bool:
        return bool(
            self.pending_candidates
            or self.promoted_this_week
            or (self.maintenance and self.maintenance.has_findings())
            or self.top_firing_rules
        )

    def as_markdown(self) -> str:
        lines: list[str] = ["# Weekly learning-rule review", ""]
        lines.append(
            f"Period: {self.period_start.date().isoformat()} → {self.period_end.date().isoformat()}"
        )
        lines.append("")

        if not self.has_anything_to_review():
            lines.append("_Nothing to review this week — your rule set is steady._")
            lines.append("")
            return "\n".join(lines)

        if self.pending_candidates:
            lines.append(f"## Pending correction candidates ({len(self.pending_candidates)})")
            lines.append("")
            lines.append(
                "_Auto-detected from chat. Review each and one-click "
                "promote / reject / edit-and-promote._"
            )
            lines.append("")
            for c in self.pending_candidates:
                conf_marker = "🔵" if c["confidence"] >= 0.8 else "⚪"
                lines.append(
                    f"- {conf_marker} `{c['id']}` (tags: {c['tags']}, conf {c['confidence']:.2f})"
                )
                lines.append(f"  - **detected**: _{c['detected_correction']}_")
                if c.get("excerpt"):
                    lines.append(f"  - **from**: _{c['excerpt'][:200]}_")
            lines.append("")

        if self.promoted_this_week:
            lines.append(f"## Rules promoted this week ({len(self.promoted_this_week)})")
            lines.append("")
            lines.append("_Sanity-check: still want these? supersede or retire if not._")
            lines.append("")
            for r in self.promoted_this_week:
                lines.append(f"- `{r['id']}` (tags: {r['tags']}): _{r['correction'][:150]}_")
            lines.append("")

        if self.maintenance:
            mr = self.maintenance
            if mr.duplicates:
                lines.append(f"## Possible duplicates ({len(mr.duplicates)})")
                lines.append("")
                for d in mr.duplicates[:10]:
                    lines.append(
                        f"- similarity {d.similarity:.2f}: "
                        f"`{d.rule_a_id[:8]}` _{d.rule_a_text[:80]}_ vs "
                        f"`{d.rule_b_id[:8]}` _{d.rule_b_text[:80]}_"
                    )
                lines.append("")
            if mr.conflicts:
                lines.append(f"## Possible conflicts ({len(mr.conflicts)})")
                lines.append("")
                for c in mr.conflicts[:10]:
                    lines.append(
                        f"- tag `{c.shared_tag}`, both start with `{c.leading_verb}`: "
                        f"`{c.rule_a_id[:8]}` vs `{c.rule_b_id[:8]}`"
                    )
                lines.append("")
            if mr.stale:
                lines.append(f"## Stale rules ({len(mr.stale)}) — haven't fired recently")
                lines.append("")
                for r in mr.stale[:10]:
                    lines.append(f"- `{r.id[:8]}` _{r.correction[:100]}_")
                lines.append("")
            if mr.compactable:
                lines.append(f"## Compactable clusters ({len(mr.compactable)})")
                lines.append("")
                for cluster in mr.compactable:
                    lines.append(f"- tag `{cluster.tag}` has {len(cluster.rule_ids)} rules")
                lines.append("")

        if self.top_firing_rules:
            lines.append(f"## Top-firing rules this week (top {len(self.top_firing_rules)})")
            lines.append("")
            for r in self.top_firing_rules:
                lines.append(f"- {r['firings']}× — `{r['id']}` _{r['correction'][:100]}_")
            lines.append("")

        return "\n".join(lines)


class WeeklyLearningReviewBuilder:
    """Assemble the weekly review from the database."""

    def __init__(
        self,
        db: Database,
        *,
        store: LearningStore | None = None,
        candidates: CorrectionCandidates | None = None,
        firings: RuleFirings | None = None,
        maintenance: MaintenanceScan | None = None,
        top_firing_count: int = 10,
    ) -> None:
        self.db = db
        self.store = store or LearningStore(db, write_ahead=False)
        self.candidates = candidates or CorrectionCandidates(db, store=self.store)
        self.firings = firings or RuleFirings(db)
        self.maintenance = maintenance or MaintenanceScan(
            db, store=self.store, firings=self.firings
        )
        self.top_firing_count = top_firing_count

    def build(self, *, ending_at: datetime | None = None) -> WeeklyLearningReview:
        end = ending_at or utcnow()
        start = end - timedelta(days=7)

        review = WeeklyLearningReview(period_start=start, period_end=end)

        # Pending candidates
        for cand in self.candidates.pending():
            review.pending_candidates.append(
                {
                    "id": cand.id[:8],
                    "tags": list(cand.inferred_skill_tags or []),
                    "confidence": cand.confidence,
                    "detected_correction": cand.detected_correction,
                    "excerpt": cand.source_excerpt,
                }
            )

        # Rules promoted this week
        with self.db.session() as s:
            recent_rules = list(
                s.exec(
                    select(LearningRule)
                    .where(LearningRule.created_at >= start)
                    .where(LearningRule.created_at <= end)
                    .where(LearningRule.superseded_by.is_(None))
                    .order_by(LearningRule.created_at.desc())
                ).all()
            )
        for r in recent_rules:
            review.promoted_this_week.append(
                {
                    "id": r.id[:8],
                    "tags": list(r.skill_tags or []),
                    "correction": r.correction,
                }
            )

        # Maintenance scan
        review.maintenance = self.maintenance.run()

        # Top-firing rules in window
        with self.db.session() as s:
            firings = list(
                s.exec(
                    select(RuleFiring)
                    .where(RuleFiring.fired_at >= start)
                    .where(RuleFiring.fired_at <= end)
                ).all()
            )
        # Count by rule_id
        counts: dict[str, int] = {}
        for f in firings:
            counts[f.rule_id] = counts.get(f.rule_id, 0) + 1
        sorted_rule_ids = sorted(counts.items(), key=lambda x: x[1], reverse=True)[
            : self.top_firing_count
        ]
        with self.db.session() as s:
            for rid, count in sorted_rule_ids:
                rule = s.get(LearningRule, rid)
                if rule is None:
                    continue
                review.top_firing_rules.append(
                    {
                        "id": rid[:8],
                        "firings": count,
                        "correction": rule.correction,
                    }
                )

        return review


__all__ = ["WeeklyLearningReview", "WeeklyLearningReviewBuilder"]
