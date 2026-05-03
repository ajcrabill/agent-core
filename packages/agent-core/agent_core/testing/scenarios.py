"""High-level scenario helpers for E2E tests.

Each helper wraps a coherent flow that crosses multiple modules — the kind
of thing a real install does dozens of times a day. They return persisted
domain objects so tests assert against actual state, not against stubs.

Use these instead of poking at individual managers when an E2E test cares
about the *flow* (inbound → obligation → plan → action). Use the per-module
APIs directly when an E2E test cares about a specific behavior of one module.
"""

from __future__ import annotations

from agent_core.state.models import (
    CorrectionCandidate,
    CorrectionCandidateStatus,
    LearningRule,
    Obligation,
    Thought,
)
from agent_core.testing.agentbed import AgentTestBed
from agent_core.work.inbound import InboundCapture


# ── Inbound capture ────────────────────────────────────────────────────────


def receive_chat(bed: AgentTestBed, *, text: str, principal: str = "test") -> Obligation:
    """Simulate a chat message from the principal landing in the inbox."""
    return InboundCapture(bed.db).capture_chat(text=text, principal=principal)


def receive_email(
    bed: AgentTestBed,
    *,
    sender: str,
    subject: str,
    body: str,
    message_id: str | None = None,
) -> Obligation:
    """Simulate an email landing in the inbox."""
    return InboundCapture(bed.db).capture_email(
        sender=sender,
        subject=subject,
        body=body,
        message_id=message_id or f"<test-{subject}>",
    )


# ── OpenBrain capture/recall ───────────────────────────────────────────────


def remember(
    bed: AgentTestBed,
    *,
    content: str,
    source_kind: str = "vault",
    source_uri: str | None = None,
) -> Thought:
    """Capture a thought into OpenBrain. Helper around store.capture()."""
    return bed.openbrain.capture(
        content,
        source_kind=source_kind,
        source_uri=source_uri,
    )


def recall(bed: AgentTestBed, *, query: str, limit: int | None = None):
    """Search OpenBrain. Returns the underlying SearchHit list."""
    return bed.openbrain.search(query, limit=limit)


# ── Supervised learning loop ───────────────────────────────────────────────


def capture_correction(
    bed: AgentTestBed,
    *,
    text: str,
    skill: str = "general",
) -> CorrectionCandidate | None:
    """Detect a correction in ``text`` and persist a candidate if found.

    Drives the full supervised-learning capture pipeline:
        text → HeuristicDetector.detect() → CorrectionCandidate row.

    Returns the persisted candidate, or None if the detector didn't see
    anything actionable in the input.
    """
    detection = bed.detector.detect(principal_message=text, skill_in_context=skill)
    if detection is None:
        return None
    return bed.candidates.propose(
        detected_correction=detection.correction_text,
        inferred_skill_tags=[skill],
        source_excerpt=detection.source_excerpt,
        confidence=detection.confidence,
    )


def promote_candidate(
    bed: AgentTestBed,
    candidate_id: str,
) -> LearningRule:
    """Promote a CorrectionCandidate to a real LearningRule."""
    return bed.candidates.promote(candidate_id)


# ── Quality / agentic feedback ─────────────────────────────────────────────


def audit_skill_run(
    bed: AgentTestBed,
    *,
    skill: str,
    task_id: str,
    output: str,
    auditor_model,
):
    """Run the auditor pipeline against a skill output.

    ``auditor_model`` should be a ``StubAuditorModel`` (or real Hermes-backed
    model) — we don't construct one here because the test wants to assert
    against the calls/score it produced.
    """
    from agent_core.quality.auditor import QualityAuditor

    auditor = QualityAuditor.from_settings(bed.settings, bed.db, auditor_model)
    return auditor.audit(
        task_type=skill,
        task_id=task_id,
        subject_model="test-skill",
        output_summary=output,
    )


__all__ = [
    "audit_skill_run",
    "capture_correction",
    "promote_candidate",
    "recall",
    "receive_chat",
    "receive_email",
    "remember",
]


# Re-export under a bare ``status`` namespace for assertions
class candidate_status:  # noqa: N801 — namespace for module-level constants
    pending = CorrectionCandidateStatus.pending
    promoted = CorrectionCandidateStatus.promoted
    rejected = CorrectionCandidateStatus.rejected
