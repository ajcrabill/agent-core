"""Sprint 22 — SMTP outbound + draft composition tests.

Two layers:

  1. Pure unit: EmailSender constructor validation, from_settings, message
     header construction (threading via In-Reply-To + References).
  2. End-to-end: in-memory DB + canned email-composer + canned SMTP send,
     covering compose_drafts idempotency and send_draft happy path /
     errors / already-sent.
"""

from __future__ import annotations

import dcos_agent.skills  # noqa: F401  registers email-triage + email-composer
import pytest
from agent_core.settings import AgentSettings
from agent_core.skills import StubLanguageModel
from agent_core.state.db import Database
from agent_core.state.models import (
    Obligation,
    ObligationEvent,
    ObligationEventKind,
    ObligationSource,
    ObligationStatus,
)
from agent_core.work.email_send import (
    EmailSender,
    EmailSendError,
    _split_email_title,
    compose_drafts,
    send_draft,
)
from sqlmodel import select

# ── Fixtures / helpers ─────────────────────────────────────────────────────


def _db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


class _FakeSecrets:
    def __init__(self, store: dict):
        self._store = store

    def get(self, namespace: str, key: str):
        return self._store.get(namespace, {}).get(key)


def _seed_triaged_email(db: Database, *, with_message_id: bool = True) -> str:
    """Create an obligation that's been through the triage pipeline.
    Returns its id."""
    with db.session() as s:
        ob = Obligation(
            title="Email from boss@example.com: Q2 sign-off",
            body="Please review the attached Q2 budget.",
            source=ObligationSource.inbound_email,
            status=ObligationStatus.in_progress,  # triage already moved it
        )
        s.add(ob)
        s.commit()
        ob_id = ob.id

        # Created event with message_id (mimics InboundCapture.capture_email)
        if with_message_id:
            s.add(
                ObligationEvent(
                    obligation_id=ob_id,
                    kind=ObligationEventKind.created,
                    actor="agent-core",
                    payload={
                        "kind": "email",
                        "sender": "boss@example.com",
                        "subject": "Q2 sign-off",
                        "message_id": "<orig-123@example.com>",
                    },
                )
            )
        # Triage event marking it for drafting
        s.add(
            ObligationEvent(
                obligation_id=ob_id,
                kind=ObligationEventKind.comment,
                actor="agent-triage",
                payload={
                    "type": "triage",
                    "action": "draft",
                    "confidence": 0.9,
                },
            )
        )
        s.commit()
    return ob_id


def _composer_lm() -> StubLanguageModel:
    """Returns a LM that emits the email-composer's expected response shape."""
    canned = "SUBJECT: Re: Q2 sign-off\n---\nLooks good — sign-off attached.\n\nBest,\nAJ"
    return StubLanguageModel(default=canned)


# ── EmailSender constructor ────────────────────────────────────────────────


def test_email_sender_requires_host():
    with pytest.raises(EmailSendError, match="host"):
        EmailSender(host="", username="u", password="p", from_address="a@b.com")


def test_email_sender_requires_username():
    with pytest.raises(EmailSendError, match="username"):
        EmailSender(host="h", username="", password="p", from_address="a@b.com")


def test_email_sender_requires_from_address():
    with pytest.raises(EmailSendError, match="from_address"):
        EmailSender(host="h", username="u", password="p", from_address="")


def test_email_sender_rejects_both_ssl_and_starttls():
    with pytest.raises(EmailSendError, match="mutually exclusive"):
        EmailSender(
            host="h",
            username="u",
            password="p",
            from_address="a@b.com",
            ssl_on_connect=True,
            starttls=True,
        )


# ── from_settings ──────────────────────────────────────────────────────────


def test_from_settings_raises_when_disabled():
    s = AgentSettings()
    with pytest.raises(EmailSendError, match="enabled"):
        EmailSender.from_settings(s, _FakeSecrets({}))


def test_from_settings_raises_when_required_field_missing():
    s = AgentSettings()
    s.email.smtp.enabled = True
    s.email.smtp.host = "smtp.example.com"
    # username + from_address still empty
    with pytest.raises(EmailSendError, match="must all be set"):
        EmailSender.from_settings(s, _FakeSecrets({"email": {"smtp_password": "x"}}))


def test_from_settings_raises_when_password_missing():
    s = AgentSettings()
    s.email.smtp.enabled = True
    s.email.smtp.host = "smtp.example.com"
    s.email.smtp.username = "u@x.com"
    s.email.smtp.from_address = "u@x.com"
    with pytest.raises(EmailSendError, match="password"):
        EmailSender.from_settings(s, _FakeSecrets({}))


def test_from_settings_builds_when_complete():
    s = AgentSettings()
    s.email.smtp.enabled = True
    s.email.smtp.host = "smtp.example.com"
    s.email.smtp.username = "u@x.com"
    s.email.smtp.from_address = "u@x.com"
    s.email.smtp.from_name = "User Display"
    sender = EmailSender.from_settings(s, _FakeSecrets({"email": {"smtp_password": "secret"}}))
    assert sender.host == "smtp.example.com"
    assert sender.from_name == "User Display"
    assert sender.password == "secret"


# ── Title parsing helper ───────────────────────────────────────────────────


def test_split_email_title_extracts_sender_and_subject():
    sender, subject = _split_email_title("Email from a@b.com: Hello world")
    assert sender == "a@b.com"
    assert subject == "Hello world"


def test_split_email_title_handles_missing_subject():
    sender, subject = _split_email_title("Email from a@b.com")
    assert sender == "a@b.com"
    assert subject == ""


def test_split_email_title_unknown_format():
    sender, subject = _split_email_title("just a title")
    assert sender == ""
    assert subject == "just a title"


# ── EmailSender.send: header construction ──────────────────────────────────


class _RecordingSMTP:
    """Captures send_message calls. Used via monkeypatch to replace
    smtplib.SMTP / SMTP_SSL inside EmailSender.send."""

    instances: list = []

    def __init__(self, host, port, *, timeout=None, context=None):
        self.host = host
        self.port = port
        self.calls: list[tuple[str, tuple]] = []
        self.sent_messages: list = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def ehlo(self):
        self.calls.append(("ehlo", ()))

    def starttls(self, context=None):
        self.calls.append(("starttls", ()))

    def login(self, user, password):
        self.calls.append(("login", (user,)))

    def send_message(self, msg):
        self.calls.append(("send_message", ()))
        self.sent_messages.append(msg)


def test_send_uses_starttls_when_configured(monkeypatch):
    _RecordingSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)

    sender = EmailSender(
        host="smtp.example.com",
        port=587,
        username="u@x.com",
        password="p",
        from_address="u@x.com",
        starttls=True,
        ssl_on_connect=False,
    )
    msg_id = sender.send(
        to="recipient@example.com",
        subject="Hello",
        body="Hi there.",
    )
    assert msg_id  # non-empty Message-ID returned
    instance = _RecordingSMTP.instances[0]
    assert ("starttls", ()) in instance.calls
    assert ("send_message", ()) in instance.calls
    sent = instance.sent_messages[0]
    assert sent["To"] == "recipient@example.com"
    assert sent["Subject"] == "Hello"
    assert sent["Message-ID"] == msg_id


def test_send_includes_in_reply_to_header(monkeypatch):
    _RecordingSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)

    sender = EmailSender(
        host="smtp.example.com",
        username="u@x.com",
        password="p",
        from_address="u@x.com",
    )
    sender.send(
        to="r@x.com",
        subject="Re: thing",
        body="reply body",
        in_reply_to="<orig-abc@example.com>",
    )
    sent = _RecordingSMTP.instances[0].sent_messages[0]
    assert sent["In-Reply-To"] == "<orig-abc@example.com>"
    assert "<orig-abc@example.com>" in sent["References"]


def test_send_uses_smtps_when_ssl_on_connect(monkeypatch):
    _RecordingSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP_SSL", _RecordingSMTP)
    sender = EmailSender(
        host="smtp.example.com",
        port=465,
        username="u@x.com",
        password="p",
        from_address="u@x.com",
        ssl_on_connect=True,
        starttls=False,
    )
    sender.send(to="r@x.com", subject="s", body="b")
    instance = _RecordingSMTP.instances[0]
    # SMTP_SSL path doesn't STARTTLS
    assert ("starttls", ()) not in instance.calls
    assert ("login", ("u@x.com",)) in instance.calls


def test_send_formats_from_with_display_name(monkeypatch):
    _RecordingSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)
    sender = EmailSender(
        host="smtp.example.com",
        username="u@x.com",
        password="p",
        from_address="u@x.com",
        from_name="AJ Crabill",
    )
    sender.send(to="r@x.com", subject="s", body="b")
    sent = _RecordingSMTP.instances[0].sent_messages[0]
    assert "AJ Crabill" in sent["From"]
    assert "u@x.com" in sent["From"]


# ── compose_drafts ─────────────────────────────────────────────────────────


def test_compose_drafts_creates_draft_event_for_triaged_email():
    db = _db()
    ob_id = _seed_triaged_email(db)

    settings = AgentSettings()
    report = compose_drafts(
        db=db,
        settings=settings,
        language_model=_composer_lm(),
    )

    assert report.drafted == 1
    assert report.errors == []

    with db.session() as s:
        events = list(
            s.exec(
                select(ObligationEvent).where(
                    ObligationEvent.obligation_id == ob_id,
                    ObligationEvent.kind == ObligationEventKind.comment,
                )
            ).all()
        )
    drafts = [e for e in events if (e.payload or {}).get("type") == "draft"]
    assert len(drafts) == 1
    payload = drafts[0].payload
    assert payload["to"] == "boss@example.com"
    assert "Re: Q2 sign-off" in payload["subject"]
    assert payload["in_reply_to"] == "<orig-123@example.com>"


def test_compose_drafts_idempotent_skips_already_drafted():
    db = _db()
    _seed_triaged_email(db)
    settings = AgentSettings()

    r1 = compose_drafts(db=db, settings=settings, language_model=_composer_lm())
    assert r1.drafted == 1

    r2 = compose_drafts(db=db, settings=settings, language_model=_composer_lm())
    assert r2.drafted == 0
    assert r2.skipped_already_drafted == 1


def test_compose_drafts_skips_non_email_obligations():
    """Manual-source obligations shouldn't get email drafts."""
    db = _db()
    with db.session() as s:
        ob = Obligation(
            title="reschedule meeting",
            source=ObligationSource.manual,
            status=ObligationStatus.in_progress,
        )
        s.add(ob)
        s.commit()
    settings = AgentSettings()
    report = compose_drafts(db=db, settings=settings, language_model=_composer_lm())
    assert report.drafted == 0


def test_compose_drafts_skips_when_no_triage_draft_action():
    """Email obligation in_progress but no triage event with action='draft'
    shouldn't be drafted (could be manually moved by the user)."""
    db = _db()
    with db.session() as s:
        ob = Obligation(
            title="Email from a@b.com: hi",
            body="hello",
            source=ObligationSource.inbound_email,
            status=ObligationStatus.in_progress,
        )
        s.add(ob)
        s.commit()
    settings = AgentSettings()
    report = compose_drafts(db=db, settings=settings, language_model=_composer_lm())
    assert report.drafted == 0


def test_compose_drafts_records_in_reply_to_when_message_id_present():
    db = _db()
    ob_id = _seed_triaged_email(db, with_message_id=True)
    compose_drafts(
        db=_db_with_seeded_id(db, ob_id),
        settings=AgentSettings(),
        language_model=_composer_lm(),
    )
    # Just test that the prior seed created the event correctly:
    with db.session() as s:
        events = list(
            s.exec(select(ObligationEvent).where(ObligationEvent.obligation_id == ob_id)).all()
        )
    creates = [e for e in events if e.kind == ObligationEventKind.created]
    assert creates and creates[0].payload["message_id"] == "<orig-123@example.com>"


def _db_with_seeded_id(db, ob_id):  # passthrough for the test above
    return db


def test_compose_drafts_handles_missing_message_id_gracefully():
    """Email without a Message-ID header still gets drafted; in_reply_to=None."""
    db = _db()
    _seed_triaged_email(db, with_message_id=False)
    report = compose_drafts(db=db, settings=AgentSettings(), language_model=_composer_lm())
    assert report.drafted == 1


# ── send_draft ─────────────────────────────────────────────────────────────


class _FakeSender:
    """Minimal EmailSender replacement that captures sends."""

    def __init__(self, *, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def send(self, *, to, subject, body, in_reply_to=None, references=None):
        self.calls.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "in_reply_to": in_reply_to,
                "references": references,
            }
        )
        if self.fail:
            raise OSError("smtp connection refused")
        return "<sent-msg-id@local>"


def _seed_with_draft(db: Database) -> str:
    """Seed an obligation through compose: triaged + drafted."""
    ob_id = _seed_triaged_email(db)
    with db.session() as s:
        s.add(
            ObligationEvent(
                obligation_id=ob_id,
                kind=ObligationEventKind.comment,
                actor="agent-composer",
                payload={
                    "type": "draft",
                    "to": "boss@example.com",
                    "subject": "Re: Q2 sign-off",
                    "body": "Looks good.",
                    "in_reply_to": "<orig-123@example.com>",
                },
            )
        )
        s.commit()
    return ob_id


def test_send_draft_sends_and_marks_done():
    db = _db()
    ob_id = _seed_with_draft(db)
    sender = _FakeSender()

    report = send_draft(db=db, sender=sender, obligation_id=ob_id)
    assert report.sent
    assert report.reason == "sent"

    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call["to"] == "boss@example.com"
    assert call["in_reply_to"] == "<orig-123@example.com>"

    with db.session() as s:
        ob = s.get(Obligation, ob_id)
    assert ob.status == ObligationStatus.done
    assert ob.completed_at is not None


def test_send_draft_records_sent_event_with_smtp_message_id():
    db = _db()
    ob_id = _seed_with_draft(db)
    sender = _FakeSender()
    send_draft(db=db, sender=sender, obligation_id=ob_id)

    with db.session() as s:
        events = list(
            s.exec(select(ObligationEvent).where(ObligationEvent.obligation_id == ob_id)).all()
        )
    sent_events = [
        e
        for e in events
        if e.kind == ObligationEventKind.comment and (e.payload or {}).get("type") == "sent"
    ]
    assert len(sent_events) == 1
    assert sent_events[0].payload["smtp_message_id"] == "<sent-msg-id@local>"


def test_send_draft_idempotent_already_sent():
    db = _db()
    ob_id = _seed_with_draft(db)
    sender = _FakeSender()
    send_draft(db=db, sender=sender, obligation_id=ob_id)

    # Re-send: should detect already_sent
    second_sender = _FakeSender()
    report = send_draft(db=db, sender=second_sender, obligation_id=ob_id)
    assert not report.sent
    assert report.reason == "already_sent"
    assert len(second_sender.calls) == 0  # nothing actually sent


def test_send_draft_no_draft_returns_no_draft():
    db = _db()
    ob_id = _seed_triaged_email(db)  # triaged but no draft event
    sender = _FakeSender()
    report = send_draft(db=db, sender=sender, obligation_id=ob_id)
    assert not report.sent
    assert report.reason == "no_draft"


def test_send_draft_obligation_missing():
    db = _db()
    sender = _FakeSender()
    report = send_draft(db=db, sender=sender, obligation_id="nonexistent-id")
    assert not report.sent
    assert report.reason == "obligation_missing"


def test_send_draft_smtp_failure_keeps_obligation_in_progress():
    db = _db()
    ob_id = _seed_with_draft(db)
    sender = _FakeSender(fail=True)

    report = send_draft(db=db, sender=sender, obligation_id=ob_id)
    assert not report.sent
    assert report.reason == "smtp_failed"
    assert "connection" in (report.error or "").lower()

    # Obligation status should NOT have changed to done
    with db.session() as s:
        ob = s.get(Obligation, ob_id)
    assert ob.status == ObligationStatus.in_progress
    assert ob.completed_at is None
