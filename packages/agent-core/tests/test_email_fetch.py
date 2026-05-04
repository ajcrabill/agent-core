"""Sprint 21 — IMAP email ingestion tests.

Two layers:

  1. Pure unit: ``_parse_message`` against a hand-rolled email.message,
     covering MIME, charsets, multipart fallbacks, and Date parsing.

  2. End-to-end: a mock IMAP connection (replacing imaplib's class on
     EmailFetcher._connect) so ``fetch_unread`` and ``fetch_and_capture``
     can be tested without a real server.
"""

from __future__ import annotations

import email
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from agent_core.settings import AgentSettings
from agent_core.state.db import Database
from agent_core.state.models import (
    Obligation,
    ObligationEvent,
    ObligationEventKind,
    ObligationSource,
    ObligationStatus,
)
from agent_core.work.email_fetch import (
    EmailFetchError,
    EmailFetcher,
    FetchedEmail,
    _decode_part,
    _extract_body,
    _parse_flags,
    _parse_message,
    _parse_uid,
    _strip_html,
    fetch_and_capture,
)
from sqlmodel import select


# ── Helpers ────────────────────────────────────────────────────────────────


def _db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


def _msg(
    *,
    sender: str = "alice@example.com",
    subject: str = "hello",
    body: str = "hi there",
    message_id: str | None = "<abc-123@example.com>",
    date: str | None = "Mon, 04 May 2026 09:00:00 +0000",
    multipart: bool = False,
    html_body: str | None = None,
    charset: str = "utf-8",
) -> bytes:
    """Build a raw RFC 822 message as bytes for fixture use."""
    if multipart:
        msg = MIMEMultipart("alternative")
        if body:
            msg.attach(MIMEText(body, "plain", charset))
        if html_body:
            msg.attach(MIMEText(html_body, "html", charset))
    else:
        msg = MIMEText(body, "plain", charset)
    msg["From"] = sender
    msg["To"] = "agent@example.com"
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if date:
        msg["Date"] = date
    return msg.as_bytes()


# ── _parse_uid / _parse_flags ──────────────────────────────────────────────


def test_parse_uid_extracts_int():
    assert _parse_uid("1 (UID 42 FLAGS (\\Seen \\Flagged))") == 42


def test_parse_uid_returns_none_when_missing():
    assert _parse_uid("1 (FLAGS ())") is None


def test_parse_flags_returns_list():
    assert _parse_flags("1 (UID 1 FLAGS (\\Seen \\Flagged))") == [
        "\\Seen",
        "\\Flagged",
    ]


def test_parse_flags_empty_when_no_flags():
    assert _parse_flags("1 (UID 1)") == []


# ── _parse_message ─────────────────────────────────────────────────────────


def test_parse_message_extracts_basic_fields():
    raw = _msg(
        sender="boss@example.com",
        subject="Q2 review",
        body="Please review the attached.",
    )
    msg = email.message_from_bytes(raw)

    parsed = _parse_message(msg, uid=42, flags=["\\Seen"])

    assert parsed.uid == 42
    assert parsed.sender == "boss@example.com"
    assert parsed.subject == "Q2 review"
    assert parsed.body == "Please review the attached."
    assert parsed.message_id == "<abc-123@example.com>"
    assert parsed.received_at is not None
    assert parsed.received_at.tzinfo is not None
    assert parsed.flags == ["\\Seen"]


def test_parse_message_handles_multipart_prefers_plain():
    raw = _msg(
        body="plain version",
        html_body="<html><body>html version</body></html>",
        multipart=True,
    )
    msg = email.message_from_bytes(raw)
    parsed = _parse_message(msg, uid=1, flags=[])
    assert "plain version" in parsed.body
    assert "html" not in parsed.body.lower()


def test_parse_message_falls_back_to_html_stripped():
    """If only HTML is present, strip tags."""
    raw = _msg(body="", html_body="<p>Hello <b>world</b></p>", multipart=True)
    msg = email.message_from_bytes(raw)
    # Drop the empty plain part so html is the only option
    for part in list(msg.walk()):
        if part.get_content_type() == "text/plain" and not part.get_payload():
            msg.set_payload([p for p in msg.get_payload() if p is not part])
            break
    parsed = _parse_message(msg, uid=1, flags=[])
    assert "Hello" in parsed.body
    assert "world" in parsed.body
    assert "<" not in parsed.body


def test_parse_message_handles_missing_message_id():
    raw = _msg(message_id=None)
    msg = email.message_from_bytes(raw)
    parsed = _parse_message(msg, uid=1, flags=[])
    assert parsed.message_id is None


def test_parse_message_handles_unparseable_date():
    raw = _msg(date="not a real date")
    msg = email.message_from_bytes(raw)
    parsed = _parse_message(msg, uid=1, flags=[])
    assert parsed.received_at is None


def test_parse_message_decodes_encoded_headers():
    """RFC 2047 encoded subject — the From / Subject headers can be
    base64/quoted-printable encoded for non-ASCII content."""
    encoded_subject = "=?utf-8?B?Q2Fmw6kgbWVldGluZw==?="  # "Café meeting"
    raw = _msg(subject=encoded_subject, body="cafe ok")
    msg = email.message_from_bytes(raw)
    parsed = _parse_message(msg, uid=1, flags=[])
    assert "Café" in parsed.subject


def test_strip_html_removes_tags_and_collapses_whitespace():
    out = _strip_html("<p>hello   <b>world</b></p>")
    assert "<" not in out
    assert "hello" in out
    assert "world" in out
    assert "  " not in out


def test_strip_html_drops_script_and_style():
    html = "<style>.x{color:red}</style><p>visible</p><script>alert(1)</script>"
    out = _strip_html(html)
    assert "alert" not in out
    assert "color:red" not in out
    assert "visible" in out


# ── EmailFetcher constructor / from_settings ──────────────────────────────


def test_fetcher_requires_host():
    with pytest.raises(EmailFetchError):
        EmailFetcher(host="", port=993, username="x", password="y")


def test_fetcher_requires_username():
    with pytest.raises(EmailFetchError):
        EmailFetcher(host="x", port=993, username="", password="y")


def test_fetcher_requires_password():
    with pytest.raises(EmailFetchError):
        EmailFetcher(host="x", port=993, username="u", password="")


class _FakeSecrets:
    """Minimal secret-store stub — get(namespace, key)."""

    def __init__(self, store: dict):
        self._store = store

    def get(self, namespace: str, key: str):
        return self._store.get(namespace, {}).get(key)


def test_from_settings_raises_when_disabled():
    s = AgentSettings()
    with pytest.raises(EmailFetchError, match="enabled"):
        EmailFetcher.from_settings(s, _FakeSecrets({}))


def test_from_settings_raises_when_host_missing():
    s = AgentSettings()
    s.email.imap.enabled = True
    s.email.imap.username = "u@example.com"
    with pytest.raises(EmailFetchError, match="host"):
        EmailFetcher.from_settings(s, _FakeSecrets({"email": {"imap_password": "x"}}))


def test_from_settings_raises_when_password_missing_in_secrets():
    s = AgentSettings()
    s.email.imap.enabled = True
    s.email.imap.host = "imap.example.com"
    s.email.imap.username = "u@example.com"
    with pytest.raises(EmailFetchError, match="password"):
        EmailFetcher.from_settings(s, _FakeSecrets({}))


def test_from_settings_builds_fetcher_when_complete():
    s = AgentSettings()
    s.email.imap.enabled = True
    s.email.imap.host = "imap.example.com"
    s.email.imap.username = "u@example.com"
    fetcher = EmailFetcher.from_settings(
        s, _FakeSecrets({"email": {"imap_password": "secret"}})
    )
    assert fetcher.host == "imap.example.com"
    assert fetcher.username == "u@example.com"
    assert fetcher.password == "secret"


# ── fetch_unread end-to-end with mock IMAP connection ─────────────────────


class _FakeIMAP:
    """Stand-in for imaplib.IMAP4 / IMAP4_SSL — captures call sequence
    and returns predictable responses."""

    def __init__(self, *, search_uids: list[int], messages: dict[int, bytes]):
        """``search_uids`` is what UID SEARCH returns; ``messages`` maps
        each UID to its raw RFC822 bytes."""
        self.search_uids = search_uids
        self.messages = messages
        self.calls: list[str] = []
        self.logged_in = False
        self.selected: str | None = None
        self.readonly: bool | None = None

    def login(self, user, password):
        self.calls.append(f"login({user!r})")
        self.logged_in = True
        return ("OK", [b""])

    def select(self, folder, readonly=False):
        self.calls.append(f"select({folder!r}, readonly={readonly})")
        self.selected = folder
        self.readonly = readonly
        return ("OK", [b""])

    def uid(self, command, *args):
        if command == "SEARCH":
            self.calls.append("uid(SEARCH)")
            data = b" ".join(str(u).encode() for u in self.search_uids)
            return ("OK", [data])
        if command == "FETCH":
            self.calls.append(f"uid(FETCH, {args[0]!r})")
            uid_list = [int(x) for x in args[0].split(b",")]
            response: list = []
            for u in uid_list:
                envelope = f"{u} (UID {u} FLAGS ())".encode()
                body = self.messages.get(u, b"")
                response.append((envelope, body))
                response.append(b")")
            return ("OK", response)
        raise NotImplementedError(command)

    def logout(self):
        self.calls.append("logout")
        return ("BYE", [b""])


def _patch_fetcher_connect(monkeypatch, fake: _FakeIMAP):
    monkeypatch.setattr(EmailFetcher, "_connect", lambda self: fake)


def test_fetch_unread_returns_empty_on_no_unseen(monkeypatch):
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )
    fake = _FakeIMAP(search_uids=[], messages={})
    _patch_fetcher_connect(monkeypatch, fake)
    assert fetcher.fetch_unread() == []
    assert "logout" in fake.calls


def test_fetch_unread_returns_parsed_message(monkeypatch):
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )
    fake = _FakeIMAP(
        search_uids=[42],
        messages={
            42: _msg(
                sender="boss@example.com",
                subject="Q2 review",
                body="Need your sign-off.",
            )
        },
    )
    _patch_fetcher_connect(monkeypatch, fake)

    out = fetcher.fetch_unread()
    assert len(out) == 1
    em = out[0]
    assert em.uid == 42
    assert em.sender == "boss@example.com"
    assert em.subject == "Q2 review"
    assert "sign-off" in em.body
    assert em.message_id == "<abc-123@example.com>"


def test_fetch_unread_orders_oldest_first(monkeypatch):
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )
    fake = _FakeIMAP(
        search_uids=[5, 1, 3],
        messages={
            1: _msg(subject="first", message_id="<m1@e.com>"),
            3: _msg(subject="middle", message_id="<m3@e.com>"),
            5: _msg(subject="last", message_id="<m5@e.com>"),
        },
    )
    _patch_fetcher_connect(monkeypatch, fake)
    out = fetcher.fetch_unread()
    uids = [e.uid for e in out]
    assert uids == [1, 3, 5]


def test_fetch_unread_respects_limit(monkeypatch):
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )
    fake = _FakeIMAP(
        search_uids=[1, 2, 3, 4, 5],
        messages={i: _msg(subject=f"m{i}", message_id=f"<m{i}@e.com>") for i in range(1, 6)},
    )
    _patch_fetcher_connect(monkeypatch, fake)
    out = fetcher.fetch_unread(limit=2)
    assert len(out) == 2
    assert [e.uid for e in out] == [1, 2]


def test_fetch_unread_uses_readonly_when_mark_read_false(monkeypatch):
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x", mark_read=False
    )
    fake = _FakeIMAP(search_uids=[], messages={})
    _patch_fetcher_connect(monkeypatch, fake)
    fetcher.fetch_unread()
    assert fake.readonly is True


def test_fetch_unread_writeable_when_mark_read_true(monkeypatch):
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x", mark_read=True
    )
    fake = _FakeIMAP(search_uids=[], messages={})
    _patch_fetcher_connect(monkeypatch, fake)
    fetcher.fetch_unread()
    assert fake.readonly is False


def test_fetch_unread_returns_empty_on_connect_failure(monkeypatch):
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )

    def _boom(self):
        raise OSError("connection refused")

    monkeypatch.setattr(EmailFetcher, "_connect", _boom)
    assert fetcher.fetch_unread() == []  # no raise into caller


# ── fetch_and_capture (idempotency, dedup) ─────────────────────────────────


def test_fetch_and_capture_creates_inbox_email_obligations(monkeypatch):
    db = _db()
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )
    fake = _FakeIMAP(
        search_uids=[1, 2],
        messages={
            1: _msg(message_id="<m1@e.com>", subject="first"),
            2: _msg(message_id="<m2@e.com>", subject="second"),
        },
    )
    _patch_fetcher_connect(monkeypatch, fake)

    report = fetch_and_capture(fetcher=fetcher, db=db)

    assert report.fetched == 2
    assert report.captured == 2
    assert report.skipped_duplicate == 0
    assert report.errors == []

    with db.session() as s:
        obs = list(s.exec(select(Obligation)).all())
    assert len(obs) == 2
    for ob in obs:
        assert ob.status == ObligationStatus.inbox
        assert ob.source == ObligationSource.inbound_email


def test_fetch_and_capture_dedupes_by_message_id(monkeypatch):
    """Second call with same Message-IDs should skip — no double capture."""
    db = _db()
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )
    fake = _FakeIMAP(
        search_uids=[1],
        messages={1: _msg(message_id="<dup@e.com>", subject="first")},
    )
    _patch_fetcher_connect(monkeypatch, fake)

    r1 = fetch_and_capture(fetcher=fetcher, db=db)
    assert r1.captured == 1

    # Second pass — same Message-ID. Server still has it as UNSEEN since
    # mark_read=False; idempotency must come from the dedup pass.
    r2 = fetch_and_capture(fetcher=fetcher, db=db)
    assert r2.fetched == 1
    assert r2.captured == 0
    assert r2.skipped_duplicate == 1

    with db.session() as s:
        obs = list(s.exec(select(Obligation)).all())
    assert len(obs) == 1


def test_fetch_and_capture_records_message_id_in_event_payload(monkeypatch):
    """The dedup logic relies on payload.message_id — make sure
    InboundCapture is actually writing it."""
    db = _db()
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )
    fake = _FakeIMAP(
        search_uids=[1],
        messages={1: _msg(message_id="<traceable@e.com>", subject="t")},
    )
    _patch_fetcher_connect(monkeypatch, fake)

    fetch_and_capture(fetcher=fetcher, db=db)

    with db.session() as s:
        events = list(
            s.exec(
                select(ObligationEvent).where(
                    ObligationEvent.kind == ObligationEventKind.created
                )
            ).all()
        )
    assert len(events) == 1
    assert events[0].payload["message_id"] == "<traceable@e.com>"


def test_fetch_and_capture_handles_message_without_id(monkeypatch):
    """A message with no Message-ID still gets captured (it's rare but
    legal); duplicate risk is acknowledged."""
    db = _db()
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )
    fake = _FakeIMAP(
        search_uids=[1],
        messages={1: _msg(message_id=None, subject="anonymous")},
    )
    _patch_fetcher_connect(monkeypatch, fake)
    report = fetch_and_capture(fetcher=fetcher, db=db)
    assert report.captured == 1
    assert report.skipped_duplicate == 0


def test_fetch_and_capture_handles_empty_inbox(monkeypatch):
    db = _db()
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )
    fake = _FakeIMAP(search_uids=[], messages={})
    _patch_fetcher_connect(monkeypatch, fake)
    report = fetch_and_capture(fetcher=fetcher, db=db)
    assert report.fetched == 0
    assert report.captured == 0


def test_fetch_and_capture_swallows_connection_errors(monkeypatch):
    """Connection failure surfaces as an empty report, not a crash."""
    db = _db()
    fetcher = EmailFetcher(
        host="imap.example.com", username="u@e.com", password="x"
    )

    def _boom(self):
        raise OSError("dns failure")

    monkeypatch.setattr(EmailFetcher, "_connect", _boom)
    report = fetch_and_capture(fetcher=fetcher, db=db)
    assert report.fetched == 0
    assert report.captured == 0
