"""SQLModel definitions for agent-core's canonical operational state.

Schema design notes:
  - One schema, two backends. Compiles to SQLite (default for dcos-agent) and
    Postgres (default for ikb-agent) without conditional logic.
  - JSON columns use SQLAlchemy's JSON type, which maps to JSONB on Postgres
    and JSON-as-TEXT on SQLite.
  - Enums are str-mixin classes stored as TEXT/VARCHAR (avoid native Postgres
    ENUM types so migrations stay portable).
  - Foreign keys use string IDs (UUID-as-text) for cross-table portability.
  - Surrogate integer PKs only for high-frequency append-only tables (events,
    rule firings, completion checks).

Per locked decision L20 (goal-directed operation):
  - Every Obligation has structured `completion_criteria`.
  - Every autonomous action will trace back to an Obligation via `obligation_id`
    on action_log (added in the next commit).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

from sqlalchemy import JSON, Column
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel


def _enum_col(enum_cls: type[Enum], *, index: bool = False, nullable: bool = False) -> Column:
    """Build an Enum SQL column with native_enum=False so both SQLite and
    Postgres store it as VARCHAR. Keeps Python-side enum validation; avoids
    Postgres-native ENUM types (which complicate migrations)."""
    return Column(
        SAEnum(enum_cls, native_enum=False, length=32, validate_strings=True),
        nullable=nullable,
        index=index,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def utcnow() -> datetime:
    """Return a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def new_id() -> str:
    """Return a new UUID4 string. Used as PK for most tables."""
    return str(uuid.uuid4())


# ── Identity ─────────────────────────────────────────────────────────────────


class Identity(SQLModel, table=True):
    """The agent's own identity. Typically exactly one row, id='self'.

    The principal is the human user; the persona is the agent's public face
    (name, email, voice). See locked decision L18.
    """

    id: str = Field(primary_key=True, default="self")
    # The agent persona
    instance_name: str = Field(description="What the user named this agent")
    persona_email: str | None = None
    persona_summary: str | None = None
    public_key: str | None = Field(
        default=None,
        description="Ed25519 pubkey for mesh-message signing",
    )
    # The human principal
    principal_name: str | None = None
    principal_email: str | None = None
    # Bookkeeping
    created_at: datetime = Field(default_factory=utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


class PeerRole(StrEnum):
    dcos = "dcos"
    ikb = "ikb"
    other = "other"


class Peer(SQLModel, table=True):
    """A discovered agent peer reachable via the mesh.

    Sprint 6 (mesh) populates this table during peer discovery / `agent-core
    peer add`.
    """

    id: str = Field(primary_key=True, default_factory=new_id)
    instance_name: str = Field(index=True)
    role: PeerRole = Field(default=PeerRole.other, sa_column=_enum_col(PeerRole, index=True))
    endpoint_url: str | None = None
    public_key: str | None = Field(
        default=None,
        description="Ed25519 pubkey for verifying inbound signed messages",
    )
    last_seen_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow, nullable=False)


# ── Work: obligations + plans ────────────────────────────────────────────────


class ObligationStatus(StrEnum):
    """The four canonical OB columns. See Sprint 0 OB consolidation."""

    inbox = "inbox"
    in_progress = "in-progress"
    waiting = "waiting"
    done = "done"


class ObligationOwner(StrEnum):
    """Who owns the next action on this obligation."""

    agent = "agent"
    principal = "principal"


class ObligationSource(StrEnum):
    """Where the obligation came from. Required per L20 — no freelance work."""

    inbound_email = "inbound_email"
    principal_chat = "principal_chat"
    peer_message = "peer_message"
    cron_trigger = "cron_trigger"
    agent_decomposition = "agent_decomposition"
    manual = "manual"


class Obligation(SQLModel, table=True):
    """A unit of work tracked on the ObligationBoard.

    Every inbound (email, chat, peer message) spawns one of these via the
    inbound capture pipeline (Sprint 3). Every autonomous action traces back
    to one. See locked decision L20.
    """

    id: str = Field(primary_key=True, default_factory=new_id)
    title: str = Field(index=True)
    body: str | None = None
    status: ObligationStatus = Field(
        default=ObligationStatus.inbox,
        sa_column=_enum_col(ObligationStatus, index=True),
    )
    owner: ObligationOwner = Field(
        default=ObligationOwner.agent,
        sa_column=_enum_col(ObligationOwner, index=True),
    )
    source: ObligationSource = Field(
        default=ObligationSource.manual,
        sa_column=_enum_col(ObligationSource),
    )
    parent_id: str | None = Field(default=None, foreign_key="obligation.id", index=True)
    # NOTE: the *currently active* plan is derived via:
    #   SELECT * FROM plan WHERE obligation_id=? AND status != 'verified'
    #   ORDER BY created_at DESC LIMIT 1
    # Avoids the FK cycle between obligation and plan.
    # JSON list of structured completion-criteria objects.
    # Schema per criterion: {"type": "<criterion_type>", ...type-specific args}
    # See completion-criteria taxonomy in docs/ARCHITECTURE.md §7.5.
    completion_criteria: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    priority: int = Field(default=0, index=True)
    due_at: datetime | None = Field(default=None, index=True)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


class ObligationEventKind(StrEnum):
    created = "created"
    status_changed = "status_changed"
    plan_developed = "plan_developed"
    step_executed = "step_executed"
    completion_checked = "completion_checked"
    closed = "closed"
    reopened = "reopened"
    comment = "comment"


class ObligationEvent(SQLModel, table=True):
    """Append-only audit log of state transitions on obligations."""

    __tablename__ = "obligation_event"

    id: int | None = Field(default=None, primary_key=True)
    obligation_id: str = Field(foreign_key="obligation.id", index=True)
    kind: ObligationEventKind = Field(sa_column=_enum_col(ObligationEventKind, index=True))
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    actor: str | None = Field(
        default=None,
        description="'agent', principal name, peer instance_name, or 'system'",
    )
    occurred_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)


class PlanStatus(StrEnum):
    planning = "planning"
    executing = "executing"
    blocked = "blocked"
    verified = "verified"


class Plan(SQLModel, table=True):
    """A plan to satisfy an obligation. One-to-one with obligation while active.

    Plans are revisable — when a step fails or the world changes, the agent
    should re-plan rather than blindly continuing.
    """

    id: str = Field(primary_key=True, default_factory=new_id)
    obligation_id: str = Field(foreign_key="obligation.id", index=True)
    # JSON list of step objects.
    # Schema per step: {"description": str, "action_class": str,
    #                   "expected_outcome": str, "depends_on": list[int]}
    steps: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    current_step: int = Field(default=0)
    status: PlanStatus = Field(
        default=PlanStatus.planning, sa_column=_enum_col(PlanStatus, index=True)
    )
    confidence: float = Field(default=0.0, description="0.0–1.0; agent self-reported")
    created_at: datetime = Field(default_factory=utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


class CompletionCheck(SQLModel, table=True):
    """Append-only log of self-tests against an obligation's completion criteria.

    Quality auditor (Sprint 4) spot-checks closed obligations against this log
    to ensure the agent isn't claiming completion without actually verifying.
    """

    __tablename__ = "completion_check"

    id: int | None = Field(default=None, primary_key=True)
    obligation_id: str = Field(foreign_key="obligation.id", index=True)
    criterion: dict[str, Any] = Field(sa_column=Column(JSON))
    passed: bool = Field(index=True)
    evidence: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    checked_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)


# ── Learning: rules, firings, candidates ─────────────────────────────────────


class LearningRule(SQLModel, table=True):
    """A supervised-learning rule. Loaded into model context by code (not
    instruction) per locked decisions L12 + L20.

    Tagging convention:
      - `skill_tags = ["general"]`  → loaded on every decision
      - `skill_tags = ["<skill>"]`  → loaded only when that skill activates
      - Multiple tags supported (rule applies to any matching skill)
    """

    __tablename__ = "learning_rule"

    id: str = Field(primary_key=True, default_factory=new_id)
    correction: str = Field(description="The rule text the agent should follow")
    skill_tags: list[str] = Field(default_factory=lambda: ["general"], sa_column=Column(JSON))
    source: str = Field(
        default="",
        description="Origin marker, e.g., 'principal chat 2026-05-02', 'pre-seed pack:professional'",
    )
    context: str = Field(default="", description="Surrounding context that produced the rule")
    notes: str = Field(default="")
    superseded_by: str | None = Field(
        default=None,
        foreign_key="learning_rule.id",
        description="If non-null, this rule has been replaced by another",
    )
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)


class RuleFiring(SQLModel, table=True):
    """One row per time a learning rule actually fires in the model context.

    Powers the 'firing visibility' and 'rules that haven't fired in 90 days'
    surfaces of the supervised-learning UX (Sprint 5b).
    """

    __tablename__ = "rule_firing"

    id: int | None = Field(default=None, primary_key=True)
    rule_id: str = Field(foreign_key="learning_rule.id", index=True)
    skill: str | None = Field(default=None, index=True)
    obligation_id: str | None = Field(
        default=None,
        foreign_key="obligation.id",
        description="Which obligation was being worked on when the rule fired",
    )
    action_summary: str | None = None
    was_overridden: bool = Field(default=False, index=True)
    fired_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)


class CorrectionCandidateStatus(StrEnum):
    pending = "pending"
    promoted = "promoted"
    rejected = "rejected"
    expired = "expired"


class CorrectionCandidate(SQLModel, table=True):
    """An auto-detected correction-from-the-principal that hasn't been promoted
    to a learning rule yet. The capture detector (Sprint 5b) writes these; the
    user reviews + one-click promotes/rejects."""

    __tablename__ = "correction_candidate"

    id: str = Field(primary_key=True, default_factory=new_id)
    detected_correction: str = Field(description="LLM-extracted rule text")
    inferred_skill_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    source_session: str | None = Field(
        default=None, description="Hermes session id where the correction was detected"
    )
    source_excerpt: str | None = Field(
        default=None, description="Verbatim user text that triggered the detection"
    )
    confidence: float = Field(default=0.0, description="Detector's confidence, 0.0–1.0")
    status: CorrectionCandidateStatus = Field(
        default=CorrectionCandidateStatus.pending,
        sa_column=_enum_col(CorrectionCandidateStatus, index=True),
    )
    promoted_to_rule_id: str | None = Field(default=None, foreign_key="learning_rule.id")
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)


# ── Delegations ──────────────────────────────────────────────────────────────


class DelegationStatus(StrEnum):
    open = "open"
    in_progress = "in-progress"
    completed = "completed"
    cancelled = "cancelled"


class Delegation(SQLModel, table=True):
    """A piece of work the agent (or principal) handed off to someone else.

    Distinct from Esby's quality-auditor undelegation (which is about taking
    work back from a model that's failing). This table is for human-or-peer
    delegations the agent must follow up on.
    """

    id: str = Field(primary_key=True, default_factory=new_id)
    obligation_id: str | None = Field(
        default=None,
        foreign_key="obligation.id",
        description="Optional link back to the obligation this delegation satisfies",
    )
    delegated_to: str = Field(
        index=True,
        description="Recipient: principal name, peer instance_name, or external person/team",
    )
    subject: str
    details: str | None = None
    due_at: datetime | None = Field(default=None, index=True)
    status: DelegationStatus = Field(
        default=DelegationStatus.open, sa_column=_enum_col(DelegationStatus, index=True)
    )
    last_check_in_at: datetime | None = None
    notes: str | None = None
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


# ── Run log + Incidents + Action log ─────────────────────────────────────────


class RunLog(SQLModel, table=True):
    """Per-skill-execution audit. One row per skill invocation (cron, on-demand,
    plan-step). Sprint 3 (`agent_core.work`) writes these."""

    __tablename__ = "run_log"

    id: int | None = Field(default=None, primary_key=True)
    skill: str = Field(index=True)
    obligation_id: str | None = Field(default=None, foreign_key="obligation.id", index=True)
    trigger: str | None = Field(default=None, description="cron|inbound|user|plan_step|peer|self")
    started_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    ended_at: datetime | None = None
    success: bool | None = Field(default=None, index=True)
    summary: str | None = None
    error: str | None = None
    metadata_json: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON), description="Free-form per-skill metadata"
    )


class IncidentSeverity(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class IncidentStatus(StrEnum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class Incident(SQLModel, table=True):
    """A failure the agent must consult before reporting an obligation 'done'.

    Per locked design: every failed tool call, cron error, or quality-audit
    failure becomes a row here. The context-loader hook (Sprint 2) surfaces
    open incidents at the top of the next prompt envelope. Agent must
    acknowledge or resolve before claiming completion on related work.
    """

    id: str = Field(primary_key=True, default_factory=new_id)
    title: str = Field(index=True)
    description: str | None = None
    severity: IncidentSeverity = Field(
        default=IncidentSeverity.medium, sa_column=_enum_col(IncidentSeverity, index=True)
    )
    status: IncidentStatus = Field(
        default=IncidentStatus.open, sa_column=_enum_col(IncidentStatus, index=True)
    )
    related_obligation_id: str | None = Field(default=None, foreign_key="obligation.id", index=True)
    source: str | None = Field(
        default=None,
        description="Where it came from: 'tool_call', 'cron', 'quality_audit', etc.",
    )
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    occurred_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None


class ActionClass(StrEnum):
    """The action-policy taxonomy from locked decision L10."""

    # Autonomous defaults
    read = "read"
    write_internal = "write_internal"
    ob_update = "ob_update"
    cross_agent_message = "cross_agent_message"
    calendar_read = "calendar_read"
    ingest = "ingest"
    capture_learning_candidate = "capture_learning_candidate"
    # Gated defaults
    send_email_external = "send_email_external"
    content_publish = "content_publish"
    calendar_invite_external = "calendar_invite_external"
    modify_people_data = "modify_people_data"
    install_skill = "install_skill"
    # Forbidden
    secret_access = "secret_access"
    finance = "finance"


class ActionOutcome(StrEnum):
    succeeded = "succeeded"
    failed = "failed"
    deferred = "deferred"
    blocked_by_policy = "blocked_by_policy"


class ActionLog(SQLModel, table=True):
    """Every autonomous action the agent takes. Per L20 (goal-directed
    operation), `obligation_id` is REQUIRED — no freelance work.

    Powers the daily digest (locked decision L9) and the supervised-learning
    firing log (cross-references `rule_firing.obligation_id`).
    """

    __tablename__ = "action_log"

    id: int | None = Field(default=None, primary_key=True)
    obligation_id: str = Field(
        foreign_key="obligation.id",
        index=True,
        description="REQUIRED per L20 — every action traces to an obligation",
    )
    action_class: ActionClass = Field(sa_column=_enum_col(ActionClass, index=True))
    target: str | None = Field(
        default=None, description="What was acted on (URL, file path, recipient, etc.)"
    )
    rationale: str | None = Field(
        default=None,
        description="Why the agent chose this action (which rules fired, "
        "which obligation step, etc.). Surfaced in daily digest.",
    )
    outcome: ActionOutcome = Field(sa_column=_enum_col(ActionOutcome, index=True))
    error: str | None = None
    occurred_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)


# ── Quality auditor ──────────────────────────────────────────────────────────


class QualityAudit(SQLModel, table=True):
    """Result of one quality-auditor evaluation. Sprint 4 populates."""

    __tablename__ = "quality_audit"

    id: str = Field(primary_key=True, default_factory=new_id)
    audit_level: int = Field(
        index=True, description="1 = primary auditor, 2 = meta-auditor (audits the auditor)"
    )
    auditor_model: str = Field(description="Model that produced this audit")
    subject_model: str = Field(index=True, description="Model whose output is being evaluated")
    task_type: str = Field(
        index=True, description="e.g., 'email-triage', 'document-creator:client-eval'"
    )
    task_id: str = Field(description="Identifier for the audited work item")
    score: float = Field(description="0.0–1.0; higher is better")
    passed: bool = Field(index=True)
    primary_notes: str | None = None
    sampling_reason: str | None = Field(
        default=None,
        description="Why this item was sampled (random, low-confidence, "
        "post-correction, smoke-suite, etc.)",
    )
    audited_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)


class QualityScore(SQLModel, table=True):
    """Running per-(model, task_type) score. Sprint 4 maintains these via
    aggregation over `quality_audit`. Auto-undelegation policy reads this
    table to decide when to stop sending work to a model."""

    __tablename__ = "quality_score"

    id: int | None = Field(default=None, primary_key=True)
    audit_level: int = Field(index=True)
    subject_model: str = Field(index=True)
    task_type: str = Field(index=True)
    total_audited: int = Field(default=0)
    running_sum: float = Field(default=0.0)
    running_avg: float = Field(default=0.0)
    last_n_avg: float | None = Field(
        default=None, description="Avg over the most recent N audits (window-based)"
    )
    strikes: int = Field(default=0, description="Consecutive scores below threshold")
    is_delegated: bool = Field(
        default=True,
        index=True,
        description="False = the auto-undelegation policy has stopped this combination",
    )
    last_audit_at: datetime | None = None
    last_undelegated_at: datetime | None = None
    last_restored_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


# ── Mesh / Intercom ──────────────────────────────────────────────────────────


class IntercomState(StrEnum):
    pending = "pending"
    delivered = "delivered"
    acknowledged = "acknowledged"


class IntercomMessage(SQLModel, table=True):
    """Inter-agent message (mesh transport store). Modeled on Esby's relay.py
    with portable schema for both sqlite + postgres backends.

    Sprint 6 (`agent_core.mesh`) implements the wire protocol over this table.
    """

    __tablename__ = "intercom_message"

    id: str = Field(primary_key=True, default_factory=new_id)
    sender: str = Field(index=True, description="instance_name of the sending agent")
    recipient: str = Field(index=True, description="instance_name of the receiving agent")
    msg_type: str = Field(default="message", description="message|question|notify|share")
    body: str = Field(default="")
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    state: IntercomState = Field(
        default=IntercomState.pending, sa_column=_enum_col(IntercomState, index=True)
    )
    ttl_seconds: int = Field(
        default=7 * 24 * 3600, description="Message TTL; expired messages are GC'd"
    )
    sent_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    signature: str | None = Field(
        default=None, description="Ed25519 signature; verifier checks against peer.public_key"
    )


class IntercomAck(SQLModel, table=True):
    """Per-message acknowledgement log. Sprint 6 maintains. Primary use: detect
    silent drops (sent but never ack'd within window)."""

    __tablename__ = "intercom_ack"

    id: int | None = Field(default=None, primary_key=True)
    message_id: str = Field(foreign_key="intercom_message.id", index=True)
    acked_by: str = Field(index=True, description="instance_name of acker")
    acked_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    note: str | None = None


# ── Sessions + Metrics ───────────────────────────────────────────────────────


class Session(SQLModel, table=True):
    """Lightweight session summary. Full transcripts stay in Hermes' state.db;
    this table stores summaries usable for cross-session continuity."""

    id: str = Field(primary_key=True, default_factory=new_id)
    started_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    ended_at: datetime | None = None
    title: str | None = None
    summary: str | None = None
    obligation_ids_touched: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    skills_invoked: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class Metric(SQLModel, table=True):
    """Generic time-series metric. Used for skill latencies, api-call counts,
    cost tracking, etc. Sprint 4.5 surfaces aggregates in the daily digest."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(
        index=True, description="dot-separated metric name, e.g., 'skill.email-triage.latency_ms'"
    )
    value: float
    tags: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    recorded_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)


# ── Content creation (Sprint 5c) ─────────────────────────────────────────────


class Exemplar(SQLModel, table=True):
    """A canonical "good output" for a content-creation skill. Synthetic items
    (per L21 batter) are flagged via `is_synthetic`."""

    id: str = Field(primary_key=True, default_factory=new_id)
    skill: str = Field(
        index=True, description="The content-creation skill this exemplar belongs to"
    )
    title: str | None = None
    content: str
    source_iteration_id: str | None = Field(
        default=None,
        foreign_key="iteration.id",
        description="The iteration that produced this exemplar (after ratification)",
    )
    is_synthetic: bool = Field(
        default=False,
        index=True,
        description="True if generated by the synthetic edge-case battery (L21)",
    )
    metadata_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    # NOTE: pgvector embedding column is added in Sprint 7 (openbrain) as a
    # backend-conditional column, allowing semantic exemplar retrieval.


class IterationStatus(StrEnum):
    in_progress = "in-progress"
    ratified = "ratified"
    abandoned = "abandoned"


class Iteration(SQLModel, table=True):
    """One (raw → attempts → corrections → final) cycle for a content-creation
    skill. Sprint 5c populates."""

    id: str = Field(primary_key=True, default_factory=new_id)
    skill: str = Field(index=True)
    obligation_id: str | None = Field(
        default=None,
        foreign_key="obligation.id",
        description="The obligation this iteration is satisfying",
    )
    raw_input: str
    attempts: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="List of {attempt_n, content, model, ts}",
    )
    corrections: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="List of {correction_n, diff, narrative, ts}",
    )
    final_content: str | None = None
    status: IterationStatus = Field(
        default=IterationStatus.in_progress,
        sa_column=_enum_col(IterationStatus, index=True),
    )
    is_synthetic: bool = Field(
        default=False,
        index=True,
        description="True if the raw_input was generated by the L21 synthetic battery",
    )
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    ratified_at: datetime | None = None


class Template(SQLModel, table=True):
    """A starting skeleton for a content-creation skill. Has placeholders the
    generation step fills in based on raw input + exemplars."""

    id: str = Field(primary_key=True, default_factory=new_id)
    skill: str = Field(index=True)
    name: str = Field(default="default")
    body: str
    placeholders: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="Names of placeholder slots in the body",
    )
    created_at: datetime = Field(default_factory=utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


class Calibration(SQLModel, table=True):
    """Per-skill calibration: confidence, ratification count, autonomous-mode
    threshold. Sprint 5c populates; quality-auditor (Sprint 4) also writes."""

    id: int | None = Field(default=None, primary_key=True)
    skill: str = Field(index=True, unique=True)
    confidence: float = Field(default=0.0)
    attempts_count: int = Field(default=0)
    ratifications_count: int = Field(default=0)
    consecutive_ratifications: int = Field(default=0)
    autonomous_mode: bool = Field(
        default=False,
        index=True,
        description="True = skill can auto-deliver; False = drafts always go to human review",
    )
    autonomous_mode_threshold: float = Field(
        default=0.85,
        description="Confidence required to flip autonomous_mode True",
    )
    last_calibrated_at: datetime | None = None


# ── OpenBrain stubs (Sprint 7 fills in vector column + ingest) ───────────────


class Thought(SQLModel, table=True):
    """A unit of semantic memory.

    Embeddings stored as JSON list of floats — portable across SQLite + Postgres
    without conditional logic, and fast enough for ~tens of thousands of thoughts
    with Python-side cosine similarity. Native vector backends (pgvector /
    sqlite-vec) come in a future sprint when scale demands.
    """

    id: str = Field(primary_key=True, default_factory=new_id)
    content: str
    fingerprint: str | None = Field(
        default=None,
        index=True,
        description="Content hash for dedup",
    )
    metadata_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(JSON),
        description="Vector embedding (JSON list of floats); None until indexed",
    )
    embedding_model: str | None = Field(
        default=None,
        index=True,
        description="Identifier for the embedding model used (e.g., 'ollama:nomic-embed-text')",
    )
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


class ThoughtSource(SQLModel, table=True):
    """Provenance for an ingested thought: where it came from, when it was
    fetched, who can see it (for iKB team-access)."""

    __tablename__ = "thought_source"

    id: int | None = Field(default=None, primary_key=True)
    thought_id: str = Field(foreign_key="thought.id", index=True)
    source_kind: str = Field(
        index=True,
        description="vault|gmail|drive|github|notion|slack|linear|bookmarks|downloads|calendar|other",
    )
    source_uri: str | None = Field(default=None, description="URL or file path of origin")
    source_title: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    authority: str | None = Field(
        default=None,
        description="canonical|reference|conjecture — feeds conflict detection",
    )
    visibility: str = Field(
        default="all",
        description="ACL hint for iKB team-access; e.g., 'all'|'admin-only'",
    )
    fetched_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)


class IngestionRun(SQLModel, table=True):
    """Per-run audit of an ingestion pipeline (e.g., gmail-sync, drive-sync)."""

    __tablename__ = "ingestion_run"

    id: str = Field(primary_key=True, default_factory=new_id)
    source_kind: str = Field(index=True)
    started_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    ended_at: datetime | None = None
    items_seen: int = Field(default=0)
    items_inserted: int = Field(default=0)
    items_updated: int = Field(default=0)
    items_skipped: int = Field(default=0)
    errors: int = Field(default=0)
    error_summary: str | None = None
    metadata_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


# ── People (relationship CRM with autonomy implications) ─────────────────────


class AutonomyOverride(StrEnum):
    """Per-person override on the install's default autonomy posture.

    inherit            — use settings.autonomy.default_policy as-is
    more_cautious      — pull this person's actions one notch toward gated
    more_aggressive    — pull one notch toward autonomous (rare; trusted
                          collaborators only)
    never_autonomous   — every action involving this person requires explicit
                          principal confirmation, regardless of preset
    """

    inherit = "inherit"
    more_cautious = "more_cautious"
    more_aggressive = "more_aggressive"
    never_autonomous = "never_autonomous"


class Person(SQLModel, table=True):
    """A human the agent has a relationship with.

    Lifted from Esby's ``people`` schema during the Sprint 13 migration. Both
    products use it: dcos-agent's "Track Charlotte in People notes" obligation
    becomes a real Person row; ikb-agent's stakeholder management is built
    around this table.

    Identity matters. Several fields encode autonomy / privacy semantics that
    skills + the action-policy enforcer respect:
      - ``autonomy_override``      raises or lowers the install's default for
                                    actions involving this person.
      - ``never_autonomous_send``  hard block on outbound autonomous messages
                                    to this person regardless of preset.
      - ``sensitive_memory_flag``  flag for the openbrain ingest layer to
                                    apply tighter visibility rules to thoughts
                                    that mention this person.

    Contact methods live in ``contact_methods`` (JSON dict, e.g.,
    ``{"email": "x@y.com", "sms": "+1...", "slack": "@handle"}``). A
    dedicated ContactMethod table can come later if the access patterns
    warrant it; today JSON is enough.
    """

    __tablename__ = "person"

    id: str = Field(primary_key=True, default_factory=new_id)
    name: str = Field(index=True, description="Display name, e.g. 'Charlotte Grinberg'")
    organization: str | None = Field(default=None, index=True)
    role: str | None = None
    stakeholder_class: str = Field(
        default="unknown_external",
        index=True,
        description=(
            "Free-form stakeholder taxonomy — product packages define the "
            "valid values (e.g., key_internal | principal_client | family_member)"
        ),
    )
    autonomy_override: AutonomyOverride = Field(
        default=AutonomyOverride.inherit,
        sa_column=_enum_col(AutonomyOverride, index=True),
    )
    relationship_intensity: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="1=acquaintance, 5=closest. Drives prioritization heuristics.",
    )
    response_sla: str | None = Field(
        default=None,
        description="Free-form SLA like '24h', '1h', 'next-business-day'",
    )
    never_autonomous_send: bool = Field(
        default=False,
        description="If True, outbound autonomous messages to this person are blocked.",
    )
    sensitive_memory_flag: bool = Field(
        default=False,
        description="If True, thoughts referencing this person get tighter visibility.",
    )
    contact_methods: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
        description=(
            "Map of method -> identifier. e.g., "
            "{'email': 'a@b.c', 'sms': '+1555...', 'slack': '@handle'}"
        ),
    )
    notes_path: str | None = Field(
        default=None,
        description="Optional pointer to a vault People note (relative path)",
    )
    metadata_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)
