"""Mesh wire types + exceptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SignatureError(Exception):
    """Raised when an inbound message's signature fails verification."""


@dataclass
class MessageEnvelope:
    """The on-the-wire format for a mesh message.

    Mirrors the IntercomMessage row but in a transport-friendly dataclass
    that can be JSON-serialized + signed independently of any DB session.

    Fields:
      id            — UUIDv4 string; assigned by sender for idempotency
      sender        — sender's instance_name
      recipient     — recipient's instance_name
      msg_type      — free-form; convention: 'message' | 'question' |
                      'notify' | 'share'
      body          — text body
      payload       — optional structured data (channel, refs, etc.)
      ttl_seconds   — recipient may garbage-collect after this
      sent_at       — ISO-8601 UTC timestamp (sender's clock)
      idempotency_key — optional sender-supplied dedup key
      signature     — base64 ed25519 signature over the canonical payload
                      (None for unsigned messages)
    """

    id: str
    sender: str
    recipient: str
    msg_type: str = "message"
    body: str = ""
    payload: dict[str, Any] | None = None
    ttl_seconds: int = 7 * 24 * 3600
    sent_at: str = ""
    idempotency_key: str | None = None
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Render to a JSON-friendly dict (for transport serialization)."""
        d: dict[str, Any] = {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "msg_type": self.msg_type,
            "body": self.body,
            "ttl_seconds": self.ttl_seconds,
            "sent_at": self.sent_at,
        }
        if self.payload is not None:
            d["payload"] = self.payload
        if self.idempotency_key is not None:
            d["idempotency_key"] = self.idempotency_key
        if self.signature is not None:
            d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageEnvelope:
        return cls(
            id=data["id"],
            sender=data["sender"],
            recipient=data["recipient"],
            msg_type=data.get("msg_type", "message"),
            body=data.get("body", ""),
            payload=data.get("payload"),
            ttl_seconds=int(data.get("ttl_seconds", 7 * 24 * 3600)),
            sent_at=data.get("sent_at", ""),
            idempotency_key=data.get("idempotency_key"),
            signature=data.get("signature"),
        )

    def signed_payload_bytes(self) -> bytes:
        """The byte-string the sender signs (and the recipient verifies)."""
        # Excludes the signature itself; keys sorted for deterministic
        # representation regardless of dict iteration order.
        from json import dumps

        payload = {k: v for k, v in self.to_dict().items() if k != "signature"}
        return dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class DeliveryResult:
    """Outcome of a send call. Returned by Transport.send()."""

    accepted: bool
    message_id: str
    duplicated: bool = False  # True if recipient said "already had this id"
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["DeliveryResult", "MessageEnvelope", "SignatureError"]
