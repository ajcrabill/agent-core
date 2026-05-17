"""Notification transports — how a single message reaches the user's phone.

Each transport satisfies the same Protocol so the dispatcher can swap them
out by config. ``NtfyTransport`` is the production default (matches the
previous build's ``loriah-crabill-urgent-7x9k`` ntfy.sh topic). ``NoopTransport``
is for tests + the ``enabled=false`` / ``transport='none'`` paths.

Adding a new transport:
    1. Implement ``send(title, body, *, priority, tags)``.
    2. Register it in the dispatcher's ``from_settings`` switch.
    3. Add a ``settings.notifications`` field for whatever it needs.

Don't reach for queueing or retries here yet — Esby's working setup just
POSTs and accepts that the occasional failure shows up in the digest. Add
durability when there's a real reason.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Protocol ────────────────────────────────────────────────────────────────


@runtime_checkable
class NotificationTransport(Protocol):
    """Push a single notification. Implementations should NOT raise on
    network errors — log and return False so the dispatcher can fall back
    or record the miss in the digest."""

    name: str

    def send(
        self,
        title: str,
        body: str,
        *,
        priority: int = 3,
        tags: list[str] | None = None,
    ) -> bool:
        """Return True if the transport accepted the message, False if it
        failed (caller decides whether that's worth surfacing)."""


# ── No-op (default when disabled) ──────────────────────────────────────────


class NoopTransport:
    """Black hole. Used when ``notifications.enabled=False`` or
    ``transport='none'``. Logs at DEBUG so you can verify wiring without
    spamming production logs."""

    name = "noop"

    def send(
        self,
        title: str,
        body: str,
        *,
        priority: int = 3,
        tags: list[str] | None = None,
    ) -> bool:
        logger.debug(
            "noop notification dropped: title=%r priority=%d tags=%r",
            title,
            priority,
            tags or [],
        )
        return True


# ── ntfy.sh ─────────────────────────────────────────────────────────────────


class NtfyTransport:
    """Push to an ntfy.sh topic via plain HTTP POST.

    ntfy maps fields like this:
        - URL path  : the topic name
        - Body      : the message body (UTF-8)
        - Title     : ``Title:`` header
        - Priority  : ``Priority:`` header (1=min, 3=default, 5=max)
        - Tags      : comma-separated ``Tags:`` header (emoji shortcodes
                      or arbitrary labels — ntfy renders them inline)

    No auth header by default — public ntfy.sh topics are unguessable-by-
    obscurity. For self-hosted ntfy with auth, wrap this class and add an
    ``Authorization`` header.
    """

    name = "ntfy"

    def __init__(
        self,
        *,
        topic: str,
        server: str = "https://ntfy.sh",
        timeout: float = 10.0,
    ) -> None:
        if not topic:
            raise ValueError("NtfyTransport requires a topic")
        self.topic = topic
        self.server = server.rstrip("/")
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return f"{self.server}/{self.topic}"

    def send(
        self,
        title: str,
        body: str,
        *,
        priority: int = 3,
        tags: list[str] | None = None,
    ) -> bool:
        if not 1 <= priority <= 5:
            raise ValueError(f"ntfy priority must be 1-5, got {priority}")
        headers = {
            "Title": title,
            "Priority": str(priority),
        }
        if tags:
            # ntfy parses comma-separated; commas in tags would be ambiguous
            # so reject them up-front rather than silently corrupt the header.
            for t in tags:
                if "," in t:
                    raise ValueError(f"ntfy tag may not contain comma: {t!r}")
            headers["Tags"] = ",".join(tags)

        req = urllib.request.Request(
            self.endpoint,
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                # ntfy returns the message JSON on success — we don't use it,
                # just confirm 2xx.
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            logger.warning("ntfy HTTP %s sending to %s: %s", e.code, self.endpoint, e.reason)
            return False
        except (urllib.error.URLError, TimeoutError) as e:
            logger.warning("ntfy transport error on %s: %s", self.endpoint, e)
            return False
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("ntfy unexpected error on %s: %s", self.endpoint, e)
            return False


__all__ = ["NoopTransport", "NotificationTransport", "NtfyTransport"]


# ── Internal: tiny helper for tests/mocks that want JSON inspection ────────


def _serialize_for_test(title: str, body: str, *, priority: int, tags: list[str] | None) -> str:
    """Stable JSON shape an in-memory test transport can write to disk."""
    return json.dumps(
        {"title": title, "body": body, "priority": priority, "tags": tags or []},
        sort_keys=True,
    )
