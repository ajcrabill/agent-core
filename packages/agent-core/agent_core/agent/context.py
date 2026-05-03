"""Context loader — code-enforced injection of state into the model's prompt.

Per locked design L20 (goal-directed operation):
  - Every inbound spawns an obligation
  - Every action traces back to one
  - The agent works on active obligations

Per the phase-A audit: the two reliability bugs (forgotten obligations,
ignored learning rules) trace to "remember to read this" instructions in
CLAUDE.md. The fix is this module — state gets injected by code on every
model invocation, not by trusting the model to look.

What gets injected, in this order:

  1. Active obligations (top-N agent-owned, not done) — so the agent always
     knows what's on its plate
  2. General learning rules (always loaded) — universal preferences
  3. Skill-scoped learning rules (when a skill is active) — situational
     preferences for this specific work
  4. Unread intercom — peer messages the agent hasn't acknowledged
  5. Open incidents — failures to consult before reporting completion

Empty blocks are dropped from the output (no point burning tokens on "no
items"). Block ordering is stable so the model sees the same structure
across invocations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_
from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import (
    Identity,
    Incident,
    IncidentStatus,
    IntercomMessage,
    IntercomState,
    LearningRule,
    Obligation,
    ObligationOwner,
    ObligationStatus,
)

# ── Types ────────────────────────────────────────────────────────────────────


@dataclass
class ContextScope:
    """What's about to happen. Tells the loader which skill-scoped rules to
    pull and (eventually) which action-class policy to apply.
    """

    skill: str | None = None
    obligation_id: str | None = None
    inbound_kind: str | None = None  # 'email' | 'chat' | 'peer_message' | None
    # Future fields: action_class, planned_action, peer_origin, etc.


@dataclass
class ContextBlock:
    """One titled chunk of context. Empty blocks are skipped at render time."""

    name: str  # short identifier, e.g., 'obligations'
    title: str  # human-readable heading, e.g., 'Active obligations (3)'
    content: str  # markdown body
    is_empty: bool = False
    # Free-form metadata — useful for tests + telemetry without polluting
    # the rendered output.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextBundle:
    """The full collection of blocks for one model invocation."""

    blocks: list[ContextBlock]

    def non_empty(self) -> list[ContextBlock]:
        return [b for b in self.blocks if not b.is_empty]

    def by_name(self, name: str) -> ContextBlock | None:
        for b in self.blocks:
            if b.name == name:
                return b
        return None

    def as_preamble(self) -> str:
        """Render the live blocks as a single markdown string suitable for
        prepending to the system prompt.

        Empty blocks are dropped. If everything is empty, returns "".
        """
        live = self.non_empty()
        if not live:
            return ""
        parts: list[str] = []
        for b in live:
            parts.append(f"## {b.title}\n\n{b.content.rstrip()}")
        return "\n\n".join(parts)


# ── Loader ───────────────────────────────────────────────────────────────────


class ContextLoader:
    """Collects context blocks from the database for a given scope.

    The collector is fast and read-only. It runs on EVERY model invocation,
    so it must stay cheap (single-digit ms even at thousands of obligations).

    Tunables:
      top_n_obligations: how many pending obligations to surface (default 8)
      intercom_unread_limit: cap on unread mesh messages shown (default 5)
      incidents_limit: cap on open incidents shown (default 5)
    """

    def __init__(
        self,
        db: Database,
        *,
        top_n_obligations: int = 8,
        intercom_unread_limit: int = 5,
        incidents_limit: int = 5,
    ) -> None:
        self.db = db
        self.top_n_obligations = top_n_obligations
        self.intercom_unread_limit = intercom_unread_limit
        self.incidents_limit = incidents_limit

    def collect(self, scope: ContextScope | None = None) -> ContextBundle:
        """Build the full context bundle for ``scope`` (or a no-skill default).

        Block order is stable: obligations, general rules, skill rules,
        intercom, incidents.
        """
        scope = scope or ContextScope()
        with self.db.session() as s:
            blocks = [
                self._obligations_block(s),
                self._general_rules_block(s),
                self._skill_rules_block(s, scope.skill),
                self._intercom_block(s),
                self._incidents_block(s),
            ]
        return ContextBundle(blocks=blocks)

    # ── Block builders ──────────────────────────────────────────────────────

    def _obligations_block(self, s) -> ContextBlock:  # type: ignore[no-untyped-def]
        """Top-N agent-owned obligations not in 'done' state.

        Sort priority: status (in-progress > waiting > inbox), then priority
        (descending), then due_at (ascending nulls last), then created_at
        (ascending — oldest gets attention first).
        """
        stmt = (
            select(Obligation)
            .where(Obligation.owner == ObligationOwner.agent)
            .where(Obligation.status != ObligationStatus.done)
        )
        rows = list(s.exec(stmt).all())
        rows.sort(key=_obligation_sort_key)
        rows = rows[: self.top_n_obligations]

        if not rows:
            return ContextBlock(
                name="obligations",
                title="Active obligations",
                content="",
                is_empty=True,
                meta={"count": 0},
            )

        lines = []
        for ob in rows:
            bits = [f"**{ob.title}**"]
            bits.append(f"_{ob.status.value}_")
            if ob.priority:
                bits.append(f"P{ob.priority}")
            if ob.due_at:
                bits.append(f"due {ob.due_at.date().isoformat()}")
            criteria_count = len(ob.completion_criteria or [])
            if criteria_count:
                bits.append(f"{criteria_count} criteria")
            lines.append(f"- {' · '.join(bits)} `id:{ob.id[:8]}`")

        return ContextBlock(
            name="obligations",
            title=f"Active obligations ({len(rows)})",
            content="\n".join(lines),
            meta={"count": len(rows), "ids": [o.id for o in rows]},
        )

    def _general_rules_block(self, s) -> ContextBlock:  # type: ignore[no-untyped-def]
        """All non-superseded learning rules tagged 'general'."""
        rules = self._active_rules_with_tag(s, "general")
        if not rules:
            return ContextBlock(
                name="general_rules",
                title="General learning rules",
                content="",
                is_empty=True,
                meta={"count": 0},
            )
        lines = [f"- {r.correction}" for r in rules]
        return ContextBlock(
            name="general_rules",
            title=f"General learning rules ({len(rules)})",
            content="\n".join(lines),
            meta={"count": len(rules), "ids": [r.id for r in rules]},
        )

    def _skill_rules_block(self, s, skill: str | None) -> ContextBlock:  # type: ignore[no-untyped-def]
        """Active rules tagged with ``skill``. Empty when no skill in scope."""
        if not skill:
            return ContextBlock(
                name="skill_rules",
                title="Skill-scoped rules",
                content="",
                is_empty=True,
                meta={"skill": None, "count": 0},
            )
        rules = self._active_rules_with_tag(s, skill)
        if not rules:
            return ContextBlock(
                name="skill_rules",
                title=f"Rules for '{skill}'",
                content="",
                is_empty=True,
                meta={"skill": skill, "count": 0},
            )
        lines = [f"- {r.correction}" for r in rules]
        return ContextBlock(
            name="skill_rules",
            title=f"Rules for '{skill}' ({len(rules)})",
            content="\n".join(lines),
            meta={"skill": skill, "count": len(rules), "ids": [r.id for r in rules]},
        )

    def _intercom_block(self, s) -> ContextBlock:  # type: ignore[no-untyped-def]
        """Unread intercom messages addressed to this instance.

        Recipient is identified by the `Identity.instance_name` row — if no
        identity row exists yet, return empty (fresh-install state).
        """
        identity = s.get(Identity, "self")
        if identity is None:
            return ContextBlock(
                name="intercom",
                title="Unread intercom",
                content="",
                is_empty=True,
                meta={"count": 0, "reason": "no identity row"},
            )

        stmt = (
            select(IntercomMessage)
            .where(IntercomMessage.recipient == identity.instance_name)
            .where(IntercomMessage.state != IntercomState.acknowledged)
            .order_by(IntercomMessage.sent_at.desc())
        )
        rows = list(s.exec(stmt).all())[: self.intercom_unread_limit]

        if not rows:
            return ContextBlock(
                name="intercom",
                title="Unread intercom",
                content="",
                is_empty=True,
                meta={"count": 0},
            )

        lines = []
        for m in rows:
            preview = (m.body or "").strip().replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:197] + "…"
            lines.append(f"- **from {m.sender}**: {preview} `id:{m.id[:8]}`")
        return ContextBlock(
            name="intercom",
            title=f"Unread intercom ({len(rows)})",
            content="\n".join(lines),
            meta={"count": len(rows), "ids": [m.id for m in rows]},
        )

    def _incidents_block(self, s) -> ContextBlock:  # type: ignore[no-untyped-def]
        """Open incidents (must be consulted before claiming completion)."""
        stmt = (
            select(Incident)
            .where(
                or_(
                    Incident.status == IncidentStatus.open,
                    Incident.status == IncidentStatus.acknowledged,
                )
            )
            .order_by(Incident.severity.desc(), Incident.occurred_at.desc())
        )
        rows = list(s.exec(stmt).all())[: self.incidents_limit]
        if not rows:
            return ContextBlock(
                name="incidents",
                title="Open incidents",
                content="",
                is_empty=True,
                meta={"count": 0},
            )
        lines = []
        for i in rows:
            bits = [f"**{i.title}**", f"_{i.severity.value}_", f"_{i.status.value}_"]
            if i.related_obligation_id:
                bits.append(f"obligation:`{i.related_obligation_id[:8]}`")
            lines.append(f"- {' · '.join(bits)} `id:{i.id[:8]}`")
        return ContextBlock(
            name="incidents",
            title=f"Open incidents ({len(rows)})",
            content="\n".join(lines),
            meta={"count": len(rows), "ids": [i.id for i in rows]},
        )

    # ── Internal: rule lookup by tag ────────────────────────────────────────

    @staticmethod
    def _active_rules_with_tag(s, tag: str) -> list[LearningRule]:  # type: ignore[no-untyped-def]
        """Return non-superseded LearningRule rows whose `skill_tags` contains
        ``tag``.

        Filtering happens in Python (skill_tags is a JSON column; portable
        across sqlite/postgres without resorting to backend-specific JSON
        operators). At expected scale (hundreds–low-thousands of rules)
        this is fine; if the rule store ever grows huge, we can add an
        indexed `skill_rule_tag` join table.
        """
        all_active = list(
            s.exec(select(LearningRule).where(LearningRule.superseded_by.is_(None))).all()
        )
        return sorted(
            (r for r in all_active if tag in (r.skill_tags or [])),
            key=lambda r: r.created_at,
            reverse=True,
        )


# ── Sort key for obligations ─────────────────────────────────────────────────


# Status → sort order (lower = higher priority)
_STATUS_ORDER = {
    ObligationStatus.in_progress: 0,
    ObligationStatus.waiting: 1,
    ObligationStatus.inbox: 2,
    ObligationStatus.done: 3,  # never appears (filtered) but defined for safety
}


def _obligation_sort_key(ob: Obligation) -> tuple:
    """Stable, deterministic sort key.

    Order:
      1. Status: in_progress > waiting > inbox
      2. Priority: higher first (so we sort by -priority)
      3. Due date: sooner first (None sorts last via tuple of (due_present, due_at))
      4. Created at: oldest first (so things don't sit forever in the queue)
    """
    due_present = 0 if ob.due_at is not None else 1
    return (
        _STATUS_ORDER.get(ob.status, 99),
        -ob.priority,
        due_present,
        ob.due_at if ob.due_at is not None else ob.created_at,
        ob.created_at,
    )


__all__ = [
    "ContextBlock",
    "ContextBundle",
    "ContextLoader",
    "ContextScope",
]
