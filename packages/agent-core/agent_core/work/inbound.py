"""Inbound capture — every inbound becomes an obligation.

L20 in two halves:
  (a) every inbound (email / chat / peer message) spawns an obligation
  (b) every action the agent takes traces back to one

This module is (a). The agent loop (Sprint 2.5) is the consumer of (b).

The capture functions are deliberately thin: they take the structured
inbound and write an Obligation row with the right `source` enum + an
ObligationEvent('created') for audit. Higher-level layers (mail, chat,
mesh) translate raw payloads into these calls.

A `default_completion_criteria` is set so the obligation can actually close
later — without explicit criteria, the agent loop's verify step would
correctly refuse to close it (per L20). Each capture function picks a
sensible default that the planning step can refine.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_core.state.db import Database
from agent_core.state.models import (
    Identity,
    Obligation,
    ObligationEvent,
    ObligationEventKind,
    ObligationOwner,
    ObligationSource,
    ObligationStatus,
)

logger = logging.getLogger(__name__)


class InboundCapture:
    """Convert inbound events into obligations.

    All capture methods:
      - Create an `Obligation` row in `status=inbox`, `owner=agent`
      - Set the appropriate `source` enum
      - Set a sensible default `completion_criteria` (planner can refine)
      - Write an `ObligationEvent` of kind `created` with payload metadata
      - Return the new Obligation
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Email ───────────────────────────────────────────────────────────────

    def capture_email(
        self,
        *,
        sender: str,
        subject: str,
        body: str,
        message_id: str | None = None,
        thread_id: str | None = None,
        received_at: str | None = None,
        priority: int = 0,
    ) -> Obligation:
        """Capture an inbound email into an obligation.

        Default criteria: principal_ratification (the agent should propose a
        triage outcome — reply / archive / file — and wait for human OK
        unless skill-policy elevates the action class to autonomous).
        """
        title = f"Email from {sender}: {subject}".strip(": ")
        if len(title) > 200:
            title = title[:197] + "…"

        ob = Obligation(
            title=title,
            body=body[:2000] if body else None,  # cap inline body; full email lives in mail store
            status=ObligationStatus.inbox,
            owner=ObligationOwner.agent,
            source=ObligationSource.inbound_email,
            priority=priority,
            completion_criteria=[
                {"type": "principal_ratification"},
            ],
        )
        self._persist(
            ob,
            event_payload={
                "kind": "email",
                "sender": sender,
                "subject": subject,
                "message_id": message_id,
                "thread_id": thread_id,
                "received_at": received_at,
            },
        )
        return ob

    # ── Principal chat ──────────────────────────────────────────────────────

    def capture_chat(
        self,
        *,
        text: str,
        principal: str | None = None,
        session_id: str | None = None,
        priority: int = 0,
        suggested_completion_criteria: list[dict] | None = None,
    ) -> Obligation:
        """Capture a chat message from the principal as an obligation.

        Default criteria: principal_ratification — the principal said something
        the agent should act on, the agent acts, and either the principal
        explicitly ratifies or the agent infers ratification from a follow-up
        message. The chat layer can pre-fill more specific criteria.
        """
        title = self._title_from_chat(text)
        ob = Obligation(
            title=title,
            body=text,
            status=ObligationStatus.inbox,
            owner=ObligationOwner.agent,
            source=ObligationSource.principal_chat,
            priority=priority,
            completion_criteria=(
                suggested_completion_criteria
                if suggested_completion_criteria is not None
                else [{"type": "principal_ratification"}]
            ),
        )
        self._persist(
            ob,
            event_payload={
                "kind": "chat",
                "principal": principal,
                "session_id": session_id,
                "preview": text[:200],
            },
        )
        return ob

    # ── Peer (mesh) message ─────────────────────────────────────────────────

    def capture_peer_message(
        self,
        *,
        sender: str,
        body: str,
        intercom_message_id: str | None = None,
        msg_type: str = "message",
        suggested_completion_criteria: list[dict] | None = None,
    ) -> Obligation:
        """Capture an inbound mesh message from a peer agent as an obligation.

        Default criteria: peer_acknowledged (we acknowledge receipt back via
        the mesh; the underlying mesh ack loop satisfies it). The peer
        protocol can supply more specific criteria via
        ``suggested_completion_criteria``.
        """
        title = f"From peer {sender}: {self._title_from_chat(body, max_chars=120)}"
        ob = Obligation(
            title=title,
            body=body,
            status=ObligationStatus.inbox,
            owner=ObligationOwner.agent,
            source=ObligationSource.peer_message,
            completion_criteria=(
                suggested_completion_criteria
                if suggested_completion_criteria is not None
                else [
                    {
                        "type": "peer_acknowledged",
                        "intercom_message_id": intercom_message_id,
                    }
                ]
            ),
        )
        self._persist(
            ob,
            event_payload={
                "kind": "peer_message",
                "sender": sender,
                "msg_type": msg_type,
                "intercom_message_id": intercom_message_id,
            },
        )
        return ob

    # ── Cron-driven obligation (e.g., daily briefing trigger) ──────────────

    def capture_cron(
        self,
        *,
        job_name: str,
        title: str,
        body: str | None = None,
        completion_criteria: list[dict] | None = None,
        priority: int = 0,
    ) -> Obligation:
        """Capture a cron tick as an obligation (e.g., 'morning briefing').

        Cron jobs that produce work for the agent should funnel through here
        so that work tracks like any other obligation.
        """
        ob = Obligation(
            title=title,
            body=body,
            status=ObligationStatus.inbox,
            owner=ObligationOwner.agent,
            source=ObligationSource.cron_trigger,
            priority=priority,
            completion_criteria=completion_criteria or [],
        )
        self._persist(
            ob,
            event_payload={
                "kind": "cron",
                "job_name": job_name,
            },
        )
        return ob

    # ── Sub-obligation (agent decomposition) ────────────────────────────────

    def capture_subtask(
        self,
        *,
        parent_id: str,
        title: str,
        body: str | None = None,
        completion_criteria: list[dict] | None = None,
        priority: int = 0,
    ) -> Obligation:
        """Capture an agent-decomposed sub-obligation under ``parent_id``.

        When a planner decides "this work needs three sub-pieces," it calls
        this for each. The parent's completion criteria typically include
        ``{"type": "subtask_closed", "obligation_id": <child id>}``.
        """
        ob = Obligation(
            title=title,
            body=body,
            status=ObligationStatus.inbox,
            owner=ObligationOwner.agent,
            source=ObligationSource.agent_decomposition,
            parent_id=parent_id,
            priority=priority,
            completion_criteria=completion_criteria or [],
        )
        self._persist(
            ob,
            event_payload={
                "kind": "subtask",
                "parent_id": parent_id,
            },
        )
        return ob

    # ── Internal ────────────────────────────────────────────────────────────

    def _persist(self, ob: Obligation, *, event_payload: dict[str, Any]) -> None:
        with self.db.session() as s:
            s.add(ob)
            s.add(
                ObligationEvent(
                    obligation_id=ob.id,
                    kind=ObligationEventKind.created,
                    actor=self._actor(),
                    payload=event_payload,
                )
            )
            s.commit()
        logger.info(
            "captured obligation %s from %s",
            ob.id,
            event_payload.get("kind", "unknown"),
        )

    def _actor(self) -> str:
        """Use the configured instance_name as actor; fall back to 'agent'."""
        with self.db.session() as s:
            ident = s.get(Identity, "self")
        return ident.instance_name if ident else "agent"

    @staticmethod
    def _title_from_chat(text: str, *, max_chars: int = 80) -> str:
        """Compress a chat/body string into a title-friendly first line."""
        first_line = text.strip().splitlines()[0] if text.strip() else "untitled"
        if len(first_line) > max_chars:
            return first_line[: max_chars - 1] + "…"
        return first_line


__all__ = ["InboundCapture"]
