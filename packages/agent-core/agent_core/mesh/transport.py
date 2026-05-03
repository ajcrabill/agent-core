"""Transport — abstract + in-process implementations.

Sprint 6a ships:
  - Transport Protocol — what the mesh client uses to actually move bytes
  - InProcessTransport — routes between agents that share a process. Used
    in tests + single-machine deployments where dcos-agent and ikb-agent
    are both Python processes on the same host.

Sprint 6b adds:
  - HttpTransport — real HTTP client/server for cross-machine deployments
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from agent_core.mesh.types import DeliveryResult, MessageEnvelope

logger = logging.getLogger(__name__)


@runtime_checkable
class Transport(Protocol):
    """Move signed envelopes from sender to recipient.

    Implementations must be idempotent on ``envelope.id`` — replays should
    return ``DeliveryResult(accepted=True, duplicated=True)``, never raise
    or double-deliver.
    """

    def send(self, envelope: MessageEnvelope) -> DeliveryResult: ...


# ── In-process transport ─────────────────────────────────────────────────────


class InProcessTransport:
    """Routes envelopes between mesh clients that share a process.

    Register each peer's MeshClient by instance_name; send() looks up the
    recipient and hands the envelope straight to their inbox.

    Used in tests and any single-machine deployment where agents share
    interpreter state.
    """

    def __init__(self) -> None:
        # Lazy-resolved by the recipient instance_name. We hold a callable so
        # we don't take a hard reference to the MeshClient (avoids circular
        # init: client constructs transport, transport needs client).
        self._receivers: dict[str, callable] = {}  # type: ignore[type-arg]

    def register_receiver(self, instance_name: str, on_inbound) -> None:  # type: ignore[no-untyped-def]
        """Attach a recipient. ``on_inbound`` is called with the envelope on
        arrival; it should return a DeliveryResult."""
        self._receivers[instance_name] = on_inbound

    def unregister_receiver(self, instance_name: str) -> None:
        self._receivers.pop(instance_name, None)

    def send(self, envelope: MessageEnvelope) -> DeliveryResult:
        receiver = self._receivers.get(envelope.recipient)
        if receiver is None:
            return DeliveryResult(
                accepted=False,
                message_id=envelope.id,
                reason=f"no receiver registered for {envelope.recipient!r}",
            )
        try:
            return receiver(envelope)
        except Exception as e:
            logger.exception("in-process transport: receiver raised")
            return DeliveryResult(
                accepted=False,
                message_id=envelope.id,
                reason=f"receiver raised: {e}",
            )


__all__ = ["InProcessTransport", "Transport"]
