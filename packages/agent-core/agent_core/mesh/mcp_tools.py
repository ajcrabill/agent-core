"""MCP-compatible tool functions over the mesh.

Mirrors the team-mcp-server tool surface from the existing intercom (per
phase-A inventory §2.3). For Sprint 6a these are plain Python functions —
the actual MCP wiring (registering them as MCP tools) lands when Hermes
vendors. The wrapping is trivial; the meaningful work is the function
implementations here.

Functions:
  team_send_message(client, recipient, body, ...)
  team_get_messages(client, since=None, sender=None, search=None, limit=20)
  team_get_thread(client, peer_instance_name, limit=None)
  team_get_daily_digest(client, period_hours=24)
  team_list_peers(client, role=None)
  team_search_messages(client, query, limit=20)
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import select

from agent_core.mesh.client import MeshClient
from agent_core.state.models import IntercomMessage, IntercomState, Peer, PeerRole, utcnow

# ── Send ─────────────────────────────────────────────────────────────────────


def team_send_message(
    client: MeshClient,
    *,
    recipient: str,
    body: str,
    msg_type: str = "message",
    payload: dict[str, Any] | None = None,
    ttl_seconds: int = 7 * 24 * 3600,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Send a message to ``recipient`` (instance_name).

    Returns a dict-shaped response (so MCP wrapping is trivial)."""
    result = client.send(
        recipient=recipient,
        body=body,
        msg_type=msg_type,
        payload=payload,
        ttl_seconds=ttl_seconds,
        idempotency_key=idempotency_key,
    )
    return {
        "accepted": result.accepted,
        "message_id": result.message_id,
        "duplicated": result.duplicated,
        "reason": result.reason,
    }


# ── Read ─────────────────────────────────────────────────────────────────────


def team_get_messages(
    client: MeshClient,
    *,
    since: datetime | None = None,
    sender: str | None = None,
    search: str | None = None,
    limit: int = 20,
    include_outbound: bool = True,
) -> list[dict[str, Any]]:
    """List messages matching the filters.

    By default includes both inbound and outbound — set
    include_outbound=False for an inbox-only view.
    """
    me = client.instance_name
    with client.db.session() as s:
        stmt = select(IntercomMessage)
        if include_outbound:
            stmt = stmt.where((IntercomMessage.recipient == me) | (IntercomMessage.sender == me))
        else:
            stmt = stmt.where(IntercomMessage.recipient == me)
        if sender:
            stmt = stmt.where(IntercomMessage.sender == sender)
        if since:
            stmt = stmt.where(IntercomMessage.sent_at >= since)
        rows = list(s.exec(stmt.order_by(IntercomMessage.sent_at.desc())).all())

    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in (r.body or "").lower()]
    rows = rows[:limit]
    return [_msg_to_dict(r) for r in rows]


def team_get_thread(
    client: MeshClient,
    *,
    peer_instance_name: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Bidirectional history with one peer (oldest first)."""
    rows = client.thread(peer_instance_name, limit=limit)
    return [_msg_to_dict(r) for r in rows]


def team_search_messages(
    client: MeshClient,
    *,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Substring search across body of all messages we sent or received."""
    return team_get_messages(
        client,
        search=query,
        limit=limit,
        include_outbound=True,
    )


# ── Daily digest ─────────────────────────────────────────────────────────────


def team_get_daily_digest(
    client: MeshClient,
    *,
    period_hours: float = 24,
) -> dict[str, Any]:
    """Summary of mesh activity in the last ``period_hours`` hours.

    Counts inbound / outbound / per-peer / per-msg-type, plus a count of
    unread inbound that's still pending acknowledgement.
    """
    me = client.instance_name
    end = utcnow()
    start = end - timedelta(hours=period_hours)

    with client.db.session() as s:
        rows = list(
            s.exec(
                select(IntercomMessage)
                .where((IntercomMessage.recipient == me) | (IntercomMessage.sender == me))
                .where(IntercomMessage.sent_at >= start)
            ).all()
        )

    inbound = [r for r in rows if r.recipient == me]
    outbound = [r for r in rows if r.sender == me]
    by_peer: dict[str, int] = {}
    for r in rows:
        peer = r.sender if r.sender != me else r.recipient
        by_peer[peer] = by_peer.get(peer, 0) + 1

    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r.msg_type] = by_type.get(r.msg_type, 0) + 1

    unread = [r for r in inbound if r.state != IntercomState.acknowledged]

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "total": len(rows),
        "inbound": len(inbound),
        "outbound": len(outbound),
        "unread_inbound": len(unread),
        "by_peer": by_peer,
        "by_msg_type": by_type,
    }


# ── Peers ────────────────────────────────────────────────────────────────────


def team_list_peers(
    client: MeshClient,
    *,
    role: PeerRole | None = None,
) -> list[dict[str, Any]]:
    """List known peers (optionally filtered by role)."""
    if role is not None:
        peers: Iterable[Peer] = client.peers.list_by_role(role)
    else:
        peers = client.peers.list_all()
    return [
        {
            "instance_name": p.instance_name,
            "role": p.role.value,
            "endpoint_url": p.endpoint_url,
            "has_public_key": bool(p.public_key),
            "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
        }
        for p in peers
    ]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _msg_to_dict(m: IntercomMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "sender": m.sender,
        "recipient": m.recipient,
        "msg_type": m.msg_type,
        "body": m.body,
        "payload": m.payload,
        "state": m.state.value,
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        "delivered_at": m.delivered_at.isoformat() if m.delivered_at else None,
        "acknowledged_at": m.acknowledged_at.isoformat() if m.acknowledged_at else None,
    }


__all__ = [
    "team_get_daily_digest",
    "team_get_messages",
    "team_get_thread",
    "team_list_peers",
    "team_search_messages",
    "team_send_message",
]
