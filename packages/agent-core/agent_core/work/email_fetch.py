"""IMAP email fetcher — turn unread inbox messages into obligations.

Sprint 21: makes auto-triage matter. Until now the inbox was filled only
by ``/capture`` chat commands or test seeds; now real mail flows in,
gets classified by the email-triage skill on the next tick, and shows up
in the digest.

Design choices:
    - **stdlib imaplib**, no new deps. Works against any IMAP server
      (Gmail with app password, Fastmail, Proton Bridge, generic).
    - **Idempotent** via Message-ID. Each captured email is tagged via
      ObligationEvent (kind=created) with payload.message_id, and the
      fetcher queries for existing message_ids before capturing.
    - **Read-only by default**. ``mark_read=True`` will set \\Seen on the
      server; default leaves the user's normal inbox view untouched. The
      Message-ID dedup means we don't depend on \\Seen for idempotency.
    - **Failure-safe**. The fetcher never raises into callers — it
      returns a ``FetchReport`` with an ``errors`` list. Network blips,
      malformed messages, charset issues all surface there without
      breaking the autonomous tick.

Threading: imaplib's connection objects are NOT thread-safe; build a
fresh one per fetch. That's also the simplest correctness story.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_core.state.db import Database


# ── Data ────────────────────────────────────────────────────────────────────


@dataclass
class FetchedEmail:
    """One message pulled from the server, parsed into the fields we need.

    ``uid`` is the IMAP UID (per-folder, monotonic). Useful for cursor-based
    fetching but NOT for cross-server dedup — use ``message_id`` for that.
    """

    uid: int
    message_id: str | None
    sender: str
    subject: str
    body: str
    received_at: datetime | None
    flags: list[str] = field(default_factory=list)


@dataclass
class FetchReport:
    """Outcome of one ``fetch_and_capture()`` call."""

    fetched: int = 0
    captured: int = 0
    skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)


# ── Errors ──────────────────────────────────────────────────────────────────


class EmailFetchError(RuntimeError):
    """Raised by EmailFetcher.from_settings when configuration is missing."""


# ── Fetcher ─────────────────────────────────────────────────────────────────


class EmailFetcher:
    """Connects to IMAP, pulls unread messages, returns parsed FetchedEmails.

    Connection lifecycle is per-call: each ``fetch_unread()`` opens, fetches,
    and closes. Suitable for cron / autonomous-tick use; less suitable for
    high-frequency polling (each call pays a TLS handshake). Acceptable
    trade-off — tick cadence is minutes, not seconds.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 993,
        ssl: bool = True,
        username: str,
        password: str,
        folder: str = "INBOX",
        timeout_seconds: float = 30.0,
        mark_read: bool = False,
    ) -> None:
        if not host:
            raise EmailFetchError("EmailFetcher requires a non-empty host")
        if not username:
            raise EmailFetchError("EmailFetcher requires a non-empty username")
        if not password:
            raise EmailFetchError("EmailFetcher requires a non-empty password")
        self.host = host
        self.port = port
        self.ssl = ssl
        self.username = username
        self.password = password
        self.folder = folder
        self.timeout_seconds = timeout_seconds
        self.mark_read = mark_read

    # ── Factory ─────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: Any, secrets: Any) -> EmailFetcher:
        """Build from ``AgentSettings`` + secret store.

        Raises ``EmailFetchError`` if email.imap.enabled is False or any
        required field is missing. Callers should catch + skip gracefully
        (the autonomous tick does this).
        """
        imap = settings.email.imap
        if not imap.enabled:
            raise EmailFetchError(
                "email.imap.enabled is False — set it via "
                "`dcos settings set email.imap.enabled=true`"
            )
        if not imap.host or not imap.username:
            raise EmailFetchError("email.imap.host and email.imap.username must be set")
        password = secrets.get("email", imap.password_secret_key)
        if not password:
            raise EmailFetchError(
                f"no email password in secrets store under "
                f"namespace='email' key='{imap.password_secret_key}'. "
                "Set it: `dcos secrets set email.imap_password=<value>` "
                "or env var AGENTCORE_EMAIL_IMAP_PASSWORD=<value>."
            )
        return cls(
            host=imap.host,
            port=imap.port,
            ssl=imap.ssl,
            username=imap.username,
            password=password,
            folder=imap.folder,
            timeout_seconds=imap.timeout_seconds,
            mark_read=imap.mark_read,
        )

    # ── Connect ─────────────────────────────────────────────────────────────

    def _connect(self) -> imaplib.IMAP4:
        """Open a fresh IMAP connection. Caller is responsible for logout."""
        if self.ssl:
            conn = imaplib.IMAP4_SSL(host=self.host, port=self.port, timeout=self.timeout_seconds)
        else:
            conn = imaplib.IMAP4(host=self.host, port=self.port, timeout=self.timeout_seconds)
        conn.login(self.username, self.password)
        return conn

    # ── Fetch ───────────────────────────────────────────────────────────────

    def fetch_unread(self, *, limit: int = 50) -> list[FetchedEmail]:
        """Return up to ``limit`` UNSEEN messages from the configured folder.

        Returns the *oldest first* so processing/triage happens in arrival
        order — the user's experience matches what they'd see in a normal
        mail client.

        Never raises into callers; on connection / auth failure returns
        an empty list and logs at WARNING.
        """
        try:
            conn = self._connect()
        except Exception as e:
            logger.warning("IMAP connect failed (%s:%s): %s", self.host, self.port, e)
            return []

        out: list[FetchedEmail] = []
        try:
            typ, _ = conn.select(self.folder, readonly=not self.mark_read)
            if typ != "OK":
                logger.warning("IMAP SELECT %s failed: %s", self.folder, typ)
                return []

            typ, data = conn.uid("SEARCH", None, "UNSEEN")
            if typ != "OK" or not data or not data[0]:
                return []

            uids = [int(x) for x in data[0].split()]
            uids.sort()  # oldest first
            uids = uids[:limit]
            if not uids:
                return []

            # Single FETCH for all UIDs is faster than N round-trips.
            uid_list = b",".join(str(u).encode() for u in uids)
            typ, msg_data = conn.uid("FETCH", uid_list, "(FLAGS RFC822)")
            if typ != "OK" or not msg_data:
                return []

            # imaplib returns a list of tuples + b')' separators. Walk it
            # carefully: each message takes exactly one tuple (envelope,
            # raw bytes) followed by one bytes element. The envelope
            # contains the UID and FLAGS; the second element is RFC822.
            current_uid: int | None = None
            current_flags: list[str] = []
            for entry in msg_data:
                if isinstance(entry, tuple):
                    envelope = entry[0].decode("utf-8", errors="replace")
                    body_bytes = entry[1]
                    current_uid = _parse_uid(envelope)
                    current_flags = _parse_flags(envelope)
                    if current_uid is None:
                        continue
                    try:
                        msg = email.message_from_bytes(body_bytes)
                        parsed = _parse_message(msg, uid=current_uid, flags=current_flags)
                        out.append(parsed)
                    except Exception as e:
                        logger.warning("failed to parse uid=%s: %s", current_uid, e)
                # Non-tuple entries (b')' separators) are ignored.
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                conn.logout()

        return out


# ── Capture (orchestrator) ──────────────────────────────────────────────────


def fetch_and_capture(
    *,
    fetcher: EmailFetcher,
    db: Database,
    limit: int = 50,
) -> FetchReport:
    """Fetch unread → dedupe by Message-ID → capture as obligations.

    Idempotency: queries existing ObligationEvents (kind=created) where
    payload.message_id matches a fetched message's Message-ID. Skips
    those. Messages without a Message-ID header (rare but possible) fall
    through and get captured fresh — duplicate risk acknowledged.

    Returns a FetchReport. Never raises — errors land in ``report.errors``.
    """
    from sqlmodel import select

    from agent_core.state.models import ObligationEvent, ObligationEventKind
    from agent_core.work.inbound import InboundCapture

    report = FetchReport()

    try:
        emails = fetcher.fetch_unread(limit=limit)
    except Exception as e:
        report.errors.append(f"fetch_unread: {e}")
        return report

    report.fetched = len(emails)
    if not emails:
        return report

    # Dedup: pull existing ObligationEvent rows whose payload contains any
    # of our message IDs. JSON-field LIKE filters work across SQLite + PG.
    candidate_message_ids = [e.message_id for e in emails if e.message_id]
    existing_message_ids: set[str] = set()
    if candidate_message_ids:
        with db.session() as s:
            rows = list(
                s.exec(
                    select(ObligationEvent).where(
                        ObligationEvent.kind == ObligationEventKind.created
                    )
                ).all()
            )
        for row in rows:
            mid = (row.payload or {}).get("message_id")
            if mid in candidate_message_ids:
                existing_message_ids.add(mid)

    capture = InboundCapture(db)
    for em in emails:
        if em.message_id and em.message_id in existing_message_ids:
            report.skipped_duplicate += 1
            continue
        try:
            capture.capture_email(
                sender=em.sender or "(unknown)",
                subject=em.subject or "(no subject)",
                body=em.body or "",
                message_id=em.message_id,
                received_at=em.received_at.isoformat() if em.received_at else None,
            )
            report.captured += 1
        except Exception as e:
            report.errors.append(f"capture uid={em.uid}: {e}")

    return report


# ── Internal: IMAP envelope parsing ────────────────────────────────────────

_UID_RE = re.compile(rb"UID (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")


def _parse_uid(envelope: str) -> int | None:
    m = _UID_RE.search(envelope.encode())
    return int(m.group(1)) if m else None


def _parse_flags(envelope: str) -> list[str]:
    m = _FLAGS_RE.search(envelope.encode())
    if not m:
        return []
    return m.group(1).decode().split()


def _parse_message(msg: Message, *, uid: int, flags: list[str]) -> FetchedEmail:
    """Pull sender/subject/body/date from a parsed email.message.Message."""
    from email.header import decode_header

    def _decode(h: str | None) -> str:
        if not h:
            return ""
        out = []
        for part, charset in decode_header(h):
            if isinstance(part, bytes):
                try:
                    out.append(part.decode(charset or "utf-8", errors="replace"))
                except (LookupError, ValueError):
                    out.append(part.decode("utf-8", errors="replace"))
            else:
                out.append(part)
        return "".join(out).strip()

    sender = _decode(msg.get("From"))
    subject = _decode(msg.get("Subject"))
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip() or None

    received_at: datetime | None = None
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            received_at = email.utils.parsedate_to_datetime(date_hdr)
            # Normalize to UTC-aware
            if received_at and received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            pass

    body = _extract_body(msg)
    return FetchedEmail(
        uid=uid,
        message_id=message_id,
        sender=sender,
        subject=subject,
        body=body,
        received_at=received_at,
        flags=flags,
    )


def _extract_body(msg: Message) -> str:
    """Pull a best-effort plain-text body from an email.

    Order of preference:
      1. text/plain part (multipart or single-part)
      2. text/html stripped of tags (very crude)
      3. Empty string
    """
    if msg.is_multipart():
        plain = None
        html = None
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = _decode_part(part)
            elif ctype == "text/html" and html is None:
                html = _decode_part(part)
        if plain:
            return plain.strip()
        if html:
            return _strip_html(html).strip()
        return ""

    ctype = msg.get_content_type()
    if ctype == "text/plain":
        return _decode_part(msg).strip()
    if ctype == "text/html":
        return _strip_html(_decode_part(msg)).strip()
    return ""


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """Crude HTML → text. We don't ship beautifulsoup just for this."""
    no_scripts = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE
    )
    no_tags = re.sub(r"<[^>]+>", " ", no_scripts)
    return re.sub(r"\s+", " ", no_tags)


__all__ = [
    "EmailFetchError",
    "EmailFetcher",
    "FetchReport",
    "FetchedEmail",
    "fetch_and_capture",
]
