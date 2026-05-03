"""agent_core.notifications — push transports + urgency-gated dispatch.

Two parts:

  - Transports: how notifications go out (``NtfyTransport``, ``NoopTransport``).
    A transport is a small Protocol — drop in Slack/Pushover/Telegram by
    satisfying it.

  - ``NotificationDispatcher``: the front door for the rest of agent-core.
    Filters by ``settings.notifications.urgency_floor`` so 'info' events
    don't wake you at 2am unless you turn the dial up.

Built around the lifted-from-Esby ntfy.sh pattern (``loriah-crabill-urgent-7x9k``
style topic). The topic is configurable in settings — pick something unguessable;
anyone with the topic can read your notifications.
"""

from agent_core.notifications.dispatcher import (
    Notification,
    NotificationDispatcher,
    NotificationFilteredError,
    Urgency,
)
from agent_core.notifications.transports import (
    NoopTransport,
    NotificationTransport,
    NtfyTransport,
)

__all__ = [
    "NoopTransport",
    "Notification",
    "NotificationDispatcher",
    "NotificationFilteredError",
    "NotificationTransport",
    "NtfyTransport",
    "Urgency",
]
