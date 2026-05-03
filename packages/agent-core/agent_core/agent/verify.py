"""Completion-criteria verification.

Per L20 design: every Obligation has structured `completion_criteria` and may
only close when every criterion verifies. Quality auditor (Sprint 4) spot-
checks closures against the `completion_check` log to catch agents claiming
completion without actually checking.

Each criterion is a JSON object:
    {"type": "<criterion_type>", ...type-specific args}

A verifier is a callable: (Database, obligation_id, criterion_dict) → CheckResult

This module provides:
  - CompletionVerifier: runs all criteria for an obligation, logs results
  - Default verifiers for the criterion types that don't need external
    integration (principal_ratification, subtask_closed, time_elapsed_with_no_
    objection)

External-integration verifiers (email_sent, calendar_event_created, etc.)
land when their respective layers do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from agent_core.state.db import Database
from agent_core.state.models import (
    CompletionCheck,
    Obligation,
    ObligationStatus,
    utcnow,
)

# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """Result of verifying one criterion. ``evidence`` is captured into the
    completion_check row for audit / quality-audit replay."""

    passed: bool
    evidence: dict[str, Any] | None = None
    note: str | None = None


@dataclass
class VerifyOutcome:
    """The full outcome of running every criterion for an obligation."""

    obligation_id: str
    all_passed: bool
    results: list[tuple[dict, CheckResult]]  # (criterion, result) pairs

    @property
    def failures(self) -> list[tuple[dict, CheckResult]]:
        return [(c, r) for c, r in self.results if not r.passed]


# ── Verifier registry ────────────────────────────────────────────────────────


VerifierFn = Callable[[Database, str, dict[str, Any]], CheckResult]


class CompletionVerifier:
    """Runs criterion verifiers and logs CompletionCheck rows.

    Default verifiers are registered at construction. Additional verifiers
    can be registered by sprints that introduce new criterion types
    (e.g., the mail layer registers ``email_sent``).
    """

    def __init__(self, db: Database, *, register_defaults: bool = True) -> None:
        self.db = db
        self._verifiers: dict[str, VerifierFn] = {}
        if register_defaults:
            self.register("principal_ratification", verify_principal_ratification)
            self.register("subtask_closed", verify_subtask_closed)
            self.register("time_elapsed_with_no_objection", verify_time_elapsed)

    def register(self, criterion_type: str, fn: VerifierFn) -> None:
        """Register or replace a verifier for a criterion type."""
        self._verifiers[criterion_type] = fn

    def has_verifier_for(self, criterion_type: str) -> bool:
        return criterion_type in self._verifiers

    def check(self, obligation_id: str) -> VerifyOutcome:
        """Run every criterion's verifier; persist a CompletionCheck row per
        criterion; return the aggregate outcome.

        Unknown criterion types fail with note='no verifier registered' so a
        loose criterion can never silently let an obligation close."""
        with self.db.session() as s:
            ob = s.get(Obligation, obligation_id)
            if ob is None:
                raise ValueError(f"obligation {obligation_id!r} not found")
            criteria = list(ob.completion_criteria or [])

        results: list[tuple[dict, CheckResult]] = []
        for criterion in criteria:
            crit_type = criterion.get("type")
            if not crit_type:
                result = CheckResult(passed=False, note="criterion has no 'type'")
            else:
                fn = self._verifiers.get(crit_type)
                if fn is None:
                    result = CheckResult(
                        passed=False, note=f"no verifier registered for {crit_type!r}"
                    )
                else:
                    try:
                        result = fn(self.db, obligation_id, criterion)
                    except Exception as e:
                        result = CheckResult(
                            passed=False,
                            note=f"verifier raised {type(e).__name__}: {e}",
                        )
            results.append((criterion, result))
            self._log_check(obligation_id, criterion, result)

        # An obligation with no criteria cannot pass — explicit criteria are
        # required per L20. (Quality auditor would otherwise have nothing to
        # spot-check.)
        all_passed = bool(criteria) and all(r.passed for _, r in results)
        return VerifyOutcome(
            obligation_id=obligation_id,
            all_passed=all_passed,
            results=results,
        )

    # ── Internals ───────────────────────────────────────────────────────────

    def _log_check(self, obligation_id: str, criterion: dict, result: CheckResult) -> None:
        with self.db.session() as s:
            s.add(
                CompletionCheck(
                    obligation_id=obligation_id,
                    criterion=criterion,
                    passed=result.passed,
                    evidence=result.evidence,
                )
            )
            s.commit()


# ── Built-in verifiers ───────────────────────────────────────────────────────


def verify_principal_ratification(db: Database, obligation_id: str, criterion: dict) -> CheckResult:
    """Check whether the principal has explicitly ratified this obligation.

    The criterion may carry an explicit ``"ratified": true`` key set by the
    chat layer when the user signals approval. Until that's wired, this
    verifier will fail with a clear note (the desired behavior — wait for
    the human).
    """
    if criterion.get("ratified") is True:
        return CheckResult(
            passed=True,
            evidence={"ratified_at": criterion.get("ratified_at")},
        )
    return CheckResult(
        passed=False,
        note="awaiting principal ratification",
    )


def verify_subtask_closed(db: Database, obligation_id: str, criterion: dict) -> CheckResult:
    """Check whether a subtask obligation has reached status='done'."""
    subtask_id = criterion.get("obligation_id") or criterion.get("subtask_id")
    if not subtask_id:
        return CheckResult(passed=False, note="criterion missing obligation_id/subtask_id")
    with db.session() as s:
        sub = s.get(Obligation, subtask_id)
        if sub is None:
            return CheckResult(
                passed=False,
                note=f"referenced subtask {subtask_id!r} not found",
            )
        passed = sub.status == ObligationStatus.done
        return CheckResult(
            passed=passed,
            evidence={"subtask_id": subtask_id, "status": sub.status.value},
        )


def verify_time_elapsed(db: Database, obligation_id: str, criterion: dict) -> CheckResult:
    """Auto-pass after N hours with no objection.

    Criterion shape:
      {"type": "time_elapsed_with_no_objection",
       "since": "<ISO ts>",
       "hours": <int>}

    If the agent intends an objection-detection signal, it can populate
    ``criterion["objection_received"] = true`` and this verifier will fail.
    """
    if criterion.get("objection_received") is True:
        return CheckResult(passed=False, note="objection received")

    since_str = criterion.get("since")
    hours = criterion.get("hours")
    if not since_str or hours is None:
        return CheckResult(passed=False, note="criterion missing since/hours")

    from datetime import datetime

    try:
        since = datetime.fromisoformat(since_str)
    except ValueError:
        return CheckResult(passed=False, note=f"invalid since timestamp {since_str!r}")

    elapsed = utcnow() - since
    required = timedelta(hours=int(hours))
    if elapsed >= required:
        return CheckResult(
            passed=True,
            evidence={
                "elapsed_hours": elapsed.total_seconds() / 3600,
                "required_hours": int(hours),
            },
        )
    return CheckResult(
        passed=False,
        note=f"only {elapsed.total_seconds() / 3600:.1f}h elapsed; need {hours}h",
    )


__all__ = [
    "CheckResult",
    "CompletionVerifier",
    "VerifierFn",
    "VerifyOutcome",
    "verify_principal_ratification",
    "verify_subtask_closed",
    "verify_time_elapsed",
]
