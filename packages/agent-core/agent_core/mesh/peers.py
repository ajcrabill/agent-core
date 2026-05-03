"""PeerRegistry — manage discovered mesh peers.

Each peer is a Peer row (defined in agent_core.state.models). Peers are
added via:
  - Wizard: `agent-core peer add ikb-bob.tail-xxxxx.ts.net`
  - Programmatic: PeerRegistry.add(...)
  - Auto-discovery (future): mDNS / Tailnet enumeration

The wire protocol uses `instance_name` as the addressing key (cross-agent
messages set `recipient = peer.instance_name`).
"""

from __future__ import annotations

import logging

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import Peer, PeerRole, utcnow

logger = logging.getLogger(__name__)


class PeerRegistry:
    """CRUD over the Peer table."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Mutators ────────────────────────────────────────────────────────────

    def add(
        self,
        *,
        instance_name: str,
        role: PeerRole = PeerRole.other,
        endpoint_url: str | None = None,
        public_key: str | None = None,
    ) -> Peer:
        """Add or update a peer (idempotent on instance_name).

        If a peer with this instance_name already exists, fields are updated
        (so re-running `peer add` with a new endpoint just refreshes it).
        """
        with self.db.session() as s:
            existing = s.exec(select(Peer).where(Peer.instance_name == instance_name)).first()
            if existing is not None:
                existing.role = role
                if endpoint_url is not None:
                    existing.endpoint_url = endpoint_url
                if public_key is not None:
                    existing.public_key = public_key
                s.add(existing)
                s.commit()
                s.refresh(existing)
                logger.info("peer updated: %s", instance_name)
                return existing

            peer = Peer(
                instance_name=instance_name,
                role=role,
                endpoint_url=endpoint_url,
                public_key=public_key,
            )
            s.add(peer)
            s.commit()
            s.refresh(peer)
            logger.info("peer added: %s (role=%s)", instance_name, role.value)
            return peer

    def remove(self, instance_name: str) -> None:
        with self.db.session() as s:
            existing = s.exec(select(Peer).where(Peer.instance_name == instance_name)).first()
            if existing is None:
                raise ValueError(f"peer {instance_name!r} not found")
            s.delete(existing)
            s.commit()

    def mark_seen(self, instance_name: str) -> Peer:
        """Update last_seen_at — called on successful inbound from this peer."""
        with self.db.session() as s:
            existing = s.exec(select(Peer).where(Peer.instance_name == instance_name)).first()
            if existing is None:
                raise ValueError(f"peer {instance_name!r} not found")
            existing.last_seen_at = utcnow()
            s.add(existing)
            s.commit()
            s.refresh(existing)
            return existing

    # ── Read API ────────────────────────────────────────────────────────────

    def get(self, instance_name: str) -> Peer | None:
        with self.db.session() as s:
            return s.exec(select(Peer).where(Peer.instance_name == instance_name)).first()

    def list_all(self) -> list[Peer]:
        with self.db.session() as s:
            return list(s.exec(select(Peer).order_by(Peer.instance_name)).all())

    def list_by_role(self, role: PeerRole) -> list[Peer]:
        with self.db.session() as s:
            return list(
                s.exec(select(Peer).where(Peer.role == role).order_by(Peer.instance_name)).all()
            )


__all__ = ["PeerRegistry"]
