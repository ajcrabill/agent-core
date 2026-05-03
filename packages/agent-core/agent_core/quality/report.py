"""Weekly quality report — aggregates the audit log into something humans read.

Esby's quality-auditor cron generates a weekly markdown report. Lifted here.
The structure is a dataclass that can also serialize to markdown.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import QualityAudit, QualityScore, utcnow


@dataclass
class TaskTypeStats:
    """Per-(model, task_type) aggregates over the report window."""

    subject_model: str
    task_type: str
    audits_in_window: int
    pass_count: int
    fail_count: int
    avg_score_in_window: float
    is_delegated: bool
    running_avg_overall: float
    last_n_avg: float | None

    @property
    def pass_rate_in_window(self) -> float:
        if self.audits_in_window == 0:
            return 0.0
        return self.pass_count / self.audits_in_window


@dataclass
class WeeklyReport:
    """A week's worth of quality data, ready to render or email."""

    period_start: datetime
    period_end: datetime
    total_audits: int = 0
    pass_rate: float = 0.0
    by_combo: list[TaskTypeStats] = field(default_factory=list)
    currently_undelegated: list[TaskTypeStats] = field(default_factory=list)

    def as_markdown(self) -> str:
        lines: list[str] = []
        lines.append("# Quality audit — weekly report")
        lines.append("")
        lines.append(
            f"Period: {self.period_start.date().isoformat()} → {self.period_end.date().isoformat()}"
        )
        lines.append("")
        lines.append(f"**Total audits**: {self.total_audits} · **Pass rate**: {self.pass_rate:.0%}")
        lines.append("")

        if self.currently_undelegated:
            lines.append("## ⚠ Currently undelegated")
            lines.append("")
            for s in self.currently_undelegated:
                lines.append(
                    f"- `{s.subject_model}` on `{s.task_type}` — "
                    f"running avg {s.running_avg_overall:.2f}, "
                    f"last-{len(self._numerator_for_label(s))} avg "
                    f"{(s.last_n_avg or 0):.2f}"
                )
            lines.append("")

        if self.by_combo:
            lines.append("## Per-(model, task_type) breakdown")
            lines.append("")
            lines.append(
                "| model | task_type | audits | pass | fail | window avg | overall avg | delegated |"
            )
            lines.append("|---|---|---:|---:|---:|---:|---:|:---:|")
            for s in sorted(
                self.by_combo,
                key=lambda x: (x.subject_model, x.task_type),
            ):
                lines.append(
                    f"| `{s.subject_model}` | `{s.task_type}` | "
                    f"{s.audits_in_window} | {s.pass_count} | {s.fail_count} | "
                    f"{s.avg_score_in_window:.2f} | "
                    f"{s.running_avg_overall:.2f} | "
                    f"{'✓' if s.is_delegated else '✗'} |"
                )
            lines.append("")

        if not self.by_combo:
            lines.append("_No audits in this period._")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _numerator_for_label(s: TaskTypeStats) -> list:
        # tiny helper used only inside the markdown generator for label width
        return list(range(min(10, s.audits_in_window or 0)))


def generate_weekly_report(
    db: Database,
    *,
    since: datetime | None = None,
    period_days: int = 7,
) -> WeeklyReport:
    """Build a WeeklyReport from the audit log.

    Defaults to the last 7 days.
    """
    end = utcnow()
    start = since or (end - timedelta(days=period_days))

    with db.session() as s:
        audits = list(
            s.exec(
                select(QualityAudit)
                .where(QualityAudit.audited_at >= start)
                .where(QualityAudit.audited_at <= end)
            ).all()
        )
        scores = list(s.exec(select(QualityScore)).all())

    score_lookup = {(r.subject_model, r.task_type): r for r in scores}

    # Bucket audits by (subject_model, task_type)
    buckets: dict[tuple[str, str], list[QualityAudit]] = defaultdict(list)
    for a in audits:
        if a.audit_level != 1:
            continue  # weekly report focuses on subject-of-work audits
        buckets[(a.subject_model, a.task_type)].append(a)

    by_combo: list[TaskTypeStats] = []
    for (model, ttype), bucket in buckets.items():
        score_row = score_lookup.get((model, ttype))
        passes = sum(1 for a in bucket if a.passed)
        avg = sum(a.score for a in bucket) / len(bucket) if bucket else 0.0
        by_combo.append(
            TaskTypeStats(
                subject_model=model,
                task_type=ttype,
                audits_in_window=len(bucket),
                pass_count=passes,
                fail_count=len(bucket) - passes,
                avg_score_in_window=avg,
                is_delegated=score_row.is_delegated if score_row else True,
                running_avg_overall=score_row.running_avg if score_row else avg,
                last_n_avg=score_row.last_n_avg if score_row else None,
            )
        )

    total = sum(s.audits_in_window for s in by_combo)
    total_pass = sum(s.pass_count for s in by_combo)
    pass_rate = (total_pass / total) if total > 0 else 0.0

    currently_undelegated = [
        TaskTypeStats(
            subject_model=r.subject_model,
            task_type=r.task_type,
            audits_in_window=0,
            pass_count=0,
            fail_count=0,
            avg_score_in_window=0.0,
            is_delegated=False,
            running_avg_overall=r.running_avg,
            last_n_avg=r.last_n_avg,
        )
        for r in scores
        if not r.is_delegated
    ]

    return WeeklyReport(
        period_start=start,
        period_end=end,
        total_audits=total,
        pass_rate=pass_rate,
        by_combo=by_combo,
        currently_undelegated=currently_undelegated,
    )


__all__ = ["TaskTypeStats", "WeeklyReport", "generate_weekly_report"]
