"""NotificationDispatcher — urgency-gated front door for push notifications.

Callers send at one of three urgency levels (info / warn / critical). The
dispatcher consults ``settings.notifications.urgency_floor`` and silently
drops anything below the floor — keeping the user's phone quiet by default
without making callers think about it.

Quiet by default:
    - ``settings.notifications.enabled = False``  → all notifications dropped.
    - ``settings.notifications.transport = "none"`` → same.
    - default ``urgency_floor = "critical"``      → only critical reaches phone.

Quietness is opt-out, not opt-in. The user wants to start strict and dial up.

Usage:

    dispatcher = NotificationDispatcher.from_settings(settings)
    dispatcher.notify(
        Notification(
            title="Esby is offline",
            body="Last heartbeat 12 minutes ago",
            urgency=Urgency.critical,
            tags=["warning"],
        )
    )

The dispatcher returns a structured ``DispatchResult`` so the caller can
log/digest the outcome — a dropped notification is recorded with the
reason ('disabled', 'below_floor', 'transport_failed').
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Literal

from agent_core.notifications.transports import (
    NoopTransport,
    NotificationTransport,
    NtfyTransport,
)


# ── Urgency ─────────────────────────────────────────────────────────────────


class Urgency(IntEnum):
    """Severity of a notification — drives ntfy priority + floor filtering.

    Integer values match the ntfy priority scale (1=lowest, 5=highest), so
    the transport doesn't have to translate.
    """

    info = 2
    warn = 3
    critical = 5

    @classmethod
    def from_string(cls, name: str) -> "Urgency":
        try:
            return cls[name]
        except KeyError as e:
            raise ValueError(
                f"unknown urgency {name!r}; expected one of: info|warn|critical"
            ) from e


# ── Notification ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Notification:
    """One thing the user might want to know about right now."""

    title: str
    body: str
    urgency: Urgency = Urgency.warn
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of a single ``notify()`` call."""

    delivered: bool
    reason: Literal["sent", "disabled", "below_floor", "transport_failed"]
    transport: str  # name of the transport that attempted (or 'noop')

    @property
    def dropped(self) -> bool:
        return not self.delivered


class NotificationFilteredError(RuntimeError):
    """Raised by ``notify_or_raise`` when a notification was filtered.

    Most callers don't want this — use ``notify()`` for fire-and-forget.
    """


# ── Dispatcher ──────────────────────────────────────────────────────────────


class NotificationDispatcher:
    """Apply urgency floor + enabled gate, then hand to the transport."""

    def __init__(
        self,
        transport: NotificationTransport,
        *,
        enabled: bool = True,
        urgency_floor: Urgency = Urgency.critical,
    ) -> None:
        self.transport = transport
        self.enabled = enabled
        self.urgency_floor = urgency_floor

    # ── Factory ─────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: object) -> "NotificationDispatcher":
        """Build from ``AgentSettings``: reads all of ``settings.notifications.*``.

        Picks ``NoopTransport`` when the user has disabled notifications or
        chosen ``transport='none'`` — that way callers never branch on
        config; they just call ``notify()`` and the dispatcher sorts it out.
        """
        n = settings.notifications  # type: ignore[attr-defined]
        floor = Urgency.from_string(n.urgency_floor)

        transport: NotificationTransport
        if not n.enabled or n.transport == "none":
            transport = NoopTransport()
        elif n.transport == "ntfy":
            if not n.ntfy_topic:
                raise ValueError(
                    "notifications.enabled=true and transport='ntfy' but "
                    "notifications.ntfy_topic is not set — set it via "
                    "`agent settings set notifications.ntfy_topic=<your-private-topic>`"
                )
            transport = NtfyTransport(topic=n.ntfy_topic, server=n.ntfy_server)
        else:
            raise ValueError(f"unknown notifications.transport: {n.transport!r}")

        return cls(transport, enabled=n.enabled, urgency_floor=floor)

    # ── Send ────────────────────────────────────────────────────────────────

    def notify(self, notif: Notification) -> DispatchResult:
        """Fire-and-forget: send if enabled + above floor; otherwise drop quietly."""
        if not self.enabled:
            return DispatchResult(delivered=False, reason="disabled", transport="noop")
        if notif.urgency < self.urgency_floor:
            return DispatchResult(
                delivered=False,
                reason="below_floor",
                transport=self.transport.name,
            )
        ok = self.transport.send(
            notif.title,
            notif.body,
            priority=int(notif.urgency),
            tags=notif.tags or None,
        )
        if ok:
            return DispatchResult(delivered=True, reason="sent", transport=self.transport.name)
        return DispatchResult(
            delivered=False, reason="transport_failed", transport=self.transport.name
        )

    def notify_or_raise(self, notif: Notification) -> DispatchResult:
        """Same as ``notify()`` but raises on filter/failure — for code paths
        that shouldn't silently swallow drops (rare; prefer ``notify()``)."""
        result = self.notify(notif)
        if result.dropped:
            raise NotificationFilteredError(
                f"notification dropped: reason={result.reason}, transport={result.transport}"
            )
        return result


__all__ = [
    "DispatchResult",
    "Notification",
    "NotificationDispatcher",
    "NotificationFilteredError",
    "Urgency",
]
