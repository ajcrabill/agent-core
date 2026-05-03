"""MeshClient — the high-level API agents use to talk to other agents.

Wires together: Identity (who am I?), PeerRegistry (who can I reach?),
Signer (proves it's me), Transport (actually sends), and the IntercomMessage
table (persists send/receive log).

Each agent process constructs ONE MeshClient and uses it for all mesh I/O.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime

from sqlmodel import select

from agent_core.mesh.peers import PeerRegistry
from agent_core.mesh.signer import Signer
from agent_core.mesh.transport import Transport
from agent_core.mesh.types import (
    DeliveryResult,
    MessageEnvelope,
    SignatureError,
)
from agent_core.state.db import Database
from agent_core.state.models import (
    Identity,
    IntercomAck,
    IntercomMessage,
    IntercomState,
    new_id,
    utcnow,
)

logger = logging.getLogger(__name__)


class MeshClient:
    """High-level send/receive/ack API for one agent.

    Construction:
      MeshClient(db, signer, transport, peers=None)

    Outbox: send() persists to IntercomMessage and asks the transport to
    deliver. On acceptance, the row's state advances to ``delivered``.

    Inbox: receive_envelope() is what InProcessTransport (or the future
    HttpServer) calls when an envelope arrives. It verifies the signature
    against the sender's known public key, persists the message in our
    local IntercomMessage table (deduplicated by id for idempotency), and
    returns a DeliveryResult.

    Ack: ack(message_id) marks one of OUR received messages as
    acknowledged (we processed it). We tell the sender via a follow-up
    `_ack` envelope so their copy advances too.
    """

    def __init__(
        self,
        db: Database,
        signer: Signer,
        transport: Transport,
        *,
        peers: PeerRegistry | None = None,
        instance_name: str | None = None,
    ) -> None:
        self.db = db
        self.signer = signer
        self.transport = transport
        self.peers = peers or PeerRegistry(db)
        self._instance_name = instance_name

    # ── Identity ────────────────────────────────────────────────────────────

    @property
    def instance_name(self) -> str:
        """Resolve our own instance_name from Identity (id='self') unless
        explicitly overridden at construction."""
        if self._instance_name is not None:
            return self._instance_name
        with self.db.session() as s:
            ident = s.get(Identity, "self")
            if ident is None:
                raise RuntimeError("no Identity row exists; create one or pass instance_name")
            return ident.instance_name

    # ── Outbox ──────────────────────────────────────────────────────────────

    def send(
        self,
        *,
        recipient: str,
        body: str,
        msg_type: str = "message",
        payload: dict | None = None,
        ttl_seconds: int = 7 * 24 * 3600,
        idempotency_key: str | None = None,
    ) -> DeliveryResult:
        """Build, sign, persist, and dispatch an outbound envelope.

        ``recipient`` is the peer's ``instance_name``. The envelope is
        recorded in our IntercomMessage table either way (so we have an
        outbox audit even if delivery fails).
        """
        envelope = MessageEnvelope(
            id=new_id(),
            sender=self.instance_name,
            recipient=recipient,
            msg_type=msg_type,
            body=body,
            payload=payload,
            ttl_seconds=ttl_seconds,
            sent_at=_iso_now(),
            idempotency_key=idempotency_key,
        )
        envelope.signature = self.signer.sign(envelope)

        # Persist outbox copy (state=pending until transport confirms)
        self._persist_outbound(envelope)

        result = self.transport.send(envelope)

        # Update state on success
        if result.accepted:
            self._advance_outbound_state(envelope.id, IntercomState.delivered)
        return result

    # ── Inbox (transport-side handler) ─────────────────────────────────────

    def receive_envelope(self, envelope: MessageEnvelope) -> DeliveryResult:
        """Called by Transport when an envelope arrives addressed to us.

        Verifies signature against the peer's public key (raises if absent
        or mismatched), then persists. Idempotent: re-delivery of the same
        id returns ``duplicated=True`` without re-inserting.
        """
        # Sanity: addressed to us?
        try:
            me = self.instance_name
        except RuntimeError:
            return DeliveryResult(
                accepted=False,
                message_id=envelope.id,
                reason="recipient identity not configured",
            )
        if envelope.recipient != me:
            return DeliveryResult(
                accepted=False,
                message_id=envelope.id,
                reason=f"recipient {envelope.recipient!r} is not me ({me!r})",
            )

        # Look up sender peer + verify signature (skip if no public key on file)
        peer = self.peers.get(envelope.sender)
        if peer is not None and peer.public_key:
            try:
                self.signer.verify(envelope, peer.public_key)
            except SignatureError as e:
                logger.warning(
                    "signature verification failed for envelope %s from %s: %s",
                    envelope.id,
                    envelope.sender,
                    e,
                )
                return DeliveryResult(
                    accepted=False,
                    message_id=envelope.id,
                    reason=f"signature verification failed: {e}",
                )

        # Idempotent persist
        with self.db.session() as s:
            existing = s.get(IntercomMessage, envelope.id)
            if existing is not None:
                return DeliveryResult(
                    accepted=True,
                    message_id=envelope.id,
                    duplicated=True,
                )
            row = IntercomMessage(
                id=envelope.id,
                sender=envelope.sender,
                recipient=envelope.recipient,
                msg_type=envelope.msg_type,
                body=envelope.body,
                payload=envelope.payload,
                ttl_seconds=envelope.ttl_seconds,
                sent_at=_parse_iso(envelope.sent_at),
                state=IntercomState.delivered,
                delivered_at=utcnow(),
                signature=envelope.signature,
            )
            s.add(row)
            s.commit()

        # Touch peer.last_seen_at if we know them
        if peer is not None:
            with contextlib.suppress(Exception):
                self.peers.mark_seen(envelope.sender)  # best-effort

        return DeliveryResult(accepted=True, message_id=envelope.id)

    # ── Read API ────────────────────────────────────────────────────────────

    def unread(self, *, limit: int | None = None) -> list[IntercomMessage]:
        """Inbound messages addressed to us that are not yet acknowledged."""
        me = self.instance_name
        with self.db.session() as s:
            stmt = (
                select(IntercomMessage)
                .where(IntercomMessage.recipient == me)
                .where(IntercomMessage.state != IntercomState.acknowledged)
                .order_by(IntercomMessage.sent_at.desc())
            )
            rows = list(s.exec(stmt).all())
        return rows[:limit] if limit else rows

    def thread(self, peer_instance_name: str, *, limit: int | None = None) -> list[IntercomMessage]:
        """Bidirectional message history with one peer (newest last for chat-
        like reading order)."""
        me = self.instance_name
        with self.db.session() as s:
            stmt = (
                select(IntercomMessage)
                .where(
                    (
                        (IntercomMessage.sender == me)
                        & (IntercomMessage.recipient == peer_instance_name)
                    )
                    | (
                        (IntercomMessage.sender == peer_instance_name)
                        & (IntercomMessage.recipient == me)
                    )
                )
                .order_by(IntercomMessage.sent_at.asc())
            )
            rows = list(s.exec(stmt).all())
        return rows[-limit:] if limit else rows

    # ── Ack ────────────────────────────────────────────────────────────────

    def ack(self, message_id: str, *, note: str | None = None) -> None:
        """Mark one of OUR received messages as fully processed (i.e., we
        acted on it). Writes IntercomAck and bumps state to acknowledged."""
        me = self.instance_name
        with self.db.session() as s:
            msg = s.get(IntercomMessage, message_id)
            if msg is None:
                raise ValueError(f"message {message_id!r} not found")
            if msg.recipient != me:
                raise ValueError(f"can't ack message {message_id!r} — not addressed to us")
            msg.state = IntercomState.acknowledged
            msg.acknowledged_at = utcnow()
            s.add(msg)
            s.add(
                IntercomAck(
                    message_id=message_id,
                    acked_by=me,
                    note=note,
                )
            )
            s.commit()

    # ── Internals ──────────────────────────────────────────────────────────

    def _persist_outbound(self, envelope: MessageEnvelope) -> None:
        with self.db.session() as s:
            existing = s.get(IntercomMessage, envelope.id)
            if existing is not None:
                return  # idempotent
            s.add(
                IntercomMessage(
                    id=envelope.id,
                    sender=envelope.sender,
                    recipient=envelope.recipient,
                    msg_type=envelope.msg_type,
                    body=envelope.body,
                    payload=envelope.payload,
                    ttl_seconds=envelope.ttl_seconds,
                    sent_at=_parse_iso(envelope.sent_at),
                    state=IntercomState.pending,
                    signature=envelope.signature,
                )
            )
            s.commit()

    def _advance_outbound_state(self, message_id: str, state: IntercomState) -> None:
        with self.db.session() as s:
            msg = s.get(IntercomMessage, message_id)
            if msg is None:
                return
            msg.state = state
            if state == IntercomState.delivered:
                msg.delivered_at = utcnow()
            s.add(msg)
            s.commit()


def _iso_now() -> str:
    return utcnow().isoformat()


def _parse_iso(s: str) -> datetime:
    """Parse the sender's ISO timestamp; tolerate missing or malformed by
    returning utcnow() — preferring liveness over strictness."""
    if not s:
        return utcnow()
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return utcnow()


__all__ = ["MeshClient"]
