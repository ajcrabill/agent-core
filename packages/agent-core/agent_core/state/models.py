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
from datetime import datetime, timezone
from enum import Enum
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
    return datetime.now(timezone.utc)


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


class PeerRole(str, Enum):
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


class ObligationStatus(str, Enum):
    """The four canonical OB columns. See Sprint 0 OB consolidation."""

    inbox = "inbox"
    in_progress = "in-progress"
    waiting = "waiting"
    done = "done"


class ObligationOwner(str, Enum):
    """Who owns the next action on this obligation."""

    agent = "agent"
    principal = "principal"


class ObligationSource(str, Enum):
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
    parent_id: str | None = Field(
        default=None, foreign_key="obligation.id", index=True
    )
    # NOTE: the *currently active* plan is derived via:
    #   SELECT * FROM plan WHERE obligation_id=? AND status != 'verified'
    #   ORDER BY created_at DESC LIMIT 1
    # Avoids the FK cycle between obligation and plan.
    # JSON list of structured completion-criteria objects.
    # Schema per criterion: {"type": "<criterion_type>", ...type-specific args}
    # See completion-criteria taxonomy in docs/ARCHITECTURE.md §7.5.
    completion_criteria: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    priority: int = Field(default=0, index=True)
    due_at: datetime | None = Field(default=None, index=True)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


class ObligationEventKind(str, Enum):
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


class PlanStatus(str, Enum):
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
    status: PlanStatus = Field(default=PlanStatus.planning, sa_column=_enum_col(PlanStatus, index=True))
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
    skill_tags: list[str] = Field(
        default_factory=lambda: ["general"], sa_column=Column(JSON)
    )
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


class CorrectionCandidateStatus(str, Enum):
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
    inferred_skill_tags: list[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    source_session: str | None = Field(
        default=None, description="Hermes session id where the correction was detected"
    )
    source_excerpt: str | None = Field(
        default=None, description="Verbatim user text that triggered the detection"
    )
    confidence: float = Field(
        default=0.0, description="Detector's confidence, 0.0–1.0"
    )
    status: CorrectionCandidateStatus = Field(
        default=CorrectionCandidateStatus.pending,
        sa_column=_enum_col(CorrectionCandidateStatus, index=True),
    )
    promoted_to_rule_id: str | None = Field(
        default=None, foreign_key="learning_rule.id"
    )
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
