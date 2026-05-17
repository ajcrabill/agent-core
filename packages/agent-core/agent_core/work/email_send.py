"""SMTP email send + draft orchestration.

Sprint 22: closes the read+write loop. Sprint 21 brought messages in;
this module sends drafted replies back out and stamps the obligation as
done.

Architecture:

  - **EmailSender** wraps stdlib smtplib (SMTPS or STARTTLS). No new deps.
  - **compose_drafts()** finds obligations that triage moved to
    in_progress (via the email-triage skill's "draft" action) but don't
    yet have a draft event, and runs the email-composer skill against
    each. The result lands as ObligationEvent kind=comment with payload
    {type: "draft", subject, body, to, in_reply_to}. Idempotent: the
    draft-event filter prevents re-drafting on every tick.
  - **send_draft()** looks up the latest draft event for an obligation,
    sends it, and on success records:
      * ObligationEvent(comment) with payload type="sent" (audit)
      * ObligationEvent(status_changed) inbox/in_progress → done
      * Sets Obligation.completed_at
    Always requires explicit user action (CLI or chat slash command).
    The autonomous tick *never* sends — only drafts.

  - **Reply threading**: SMTP send sets In-Reply-To + References headers
    using the original message's Message-ID. So the recipient sees a
    threaded reply, not a new conversation.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_core.state.db import Database


# ── Errors ──────────────────────────────────────────────────────────────────


class EmailSendError(RuntimeError):
    """Raised by EmailSender.from_settings when configuration is missing."""


# ── Reports ─────────────────────────────────────────────────────────────────


@dataclass
class ComposeReport:
    """Outcome of one ``compose_drafts()`` call."""

    candidates: int = 0
    drafted: int = 0
    skipped_already_drafted: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SendReport:
    """Outcome of one ``send_draft()`` call."""

    sent: bool
    reason: str  # "sent" | "no_draft" | "already_sent" | "smtp_failed" | "obligation_missing"
    obligation_id: str | None = None
    to: str | None = None
    subject: str | None = None
    error: str | None = None


# ── EmailSender ─────────────────────────────────────────────────────────────


class EmailSender:
    """Connect → send → close, one message at a time.

    Same lifecycle pattern as EmailFetcher — fresh connection per send.
    Tick cadence is minutes; SMTP TLS overhead is negligible at that rate.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        ssl_on_connect: bool = False,
        starttls: bool = True,
        username: str,
        password: str,
        from_address: str,
        from_name: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not host:
            raise EmailSendError("EmailSender requires a non-empty host")
        if not username or not password:
            raise EmailSendError("EmailSender requires username and password")
        if not from_address:
            raise EmailSendError("EmailSender requires from_address")
        if ssl_on_connect and starttls:
            raise EmailSendError("ssl_on_connect and starttls are mutually exclusive — pick one")
        self.host = host
        self.port = port
        self.ssl_on_connect = ssl_on_connect
        self.starttls = starttls
        self.username = username
        self.password = password
        self.from_address = from_address
        self.from_name = from_name
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: Any, secrets: Any) -> EmailSender:
        smtp = settings.email.smtp
        if not smtp.enabled:
            raise EmailSendError(
                "email.smtp.enabled is False — set it via "
                "`dcos settings set email.smtp.enabled=true`"
            )
        if not smtp.host or not smtp.username or not smtp.from_address:
            raise EmailSendError("email.smtp.host, .username, and .from_address must all be set")
        password = secrets.get("email", smtp.password_secret_key)
        if not password:
            raise EmailSendError(
                f"no SMTP password in secrets store under "
                f"namespace='email' key='{smtp.password_secret_key}'. "
                f"Set it: `dcos secrets set email.{smtp.password_secret_key}=<value>`."
            )
        return cls(
            host=smtp.host,
            port=smtp.port,
            ssl_on_connect=smtp.ssl,
            starttls=smtp.starttls,
            username=smtp.username,
            password=password,
            from_address=smtp.from_address,
            from_name=smtp.from_name,
            timeout_seconds=smtp.timeout_seconds,
        )

    # ── Send ────────────────────────────────────────────────────────────────

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        references: list[str] | None = None,
    ) -> str:
        """Send a single message. Returns the Message-ID it generated.

        Raises ``smtplib`` exceptions on failure; callers are expected to
        wrap and surface as SendReport.

        ``in_reply_to``: the Message-ID of the message being replied to.
        Sets both In-Reply-To and prepends to References so threading
        survives in the recipient's mail client.
        """
        msg = EmailMessage()
        msg["From"] = (
            formataddr((self.from_name, self.from_address)) if self.from_name else self.from_address
        )
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg_id = make_msgid()
        msg["Message-ID"] = msg_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            ref_chain = list(references or [])
            if in_reply_to not in ref_chain:
                ref_chain.append(in_reply_to)
            msg["References"] = " ".join(ref_chain)

        msg.set_content(body)

        if self.ssl_on_connect:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                self.host, self.port, context=ctx, timeout=self.timeout_seconds
            ) as conn:
                conn.login(self.username, self.password)
                conn.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as conn:
                conn.ehlo()
                if self.starttls:
                    ctx = ssl.create_default_context()
                    conn.starttls(context=ctx)
                    conn.ehlo()
                conn.login(self.username, self.password)
                conn.send_message(msg)

        return msg_id


# ── Compose orchestration ──────────────────────────────────────────────────


def compose_drafts(
    *,
    db: Database,
    settings: Any,
    language_model: Any | None = None,
    limit: int = 10,
) -> ComposeReport:
    """Find triaged-as-draft email obligations and run email-composer.

    A "triaged-as-draft" obligation has:
      - source = inbound_email
      - status = in-progress (the triage step transitions "draft"→in_progress)
      - an agent-triage ObligationEvent with payload.action='draft'
      - NO existing draft ObligationEvent (kind=comment, payload.type='draft')

    For each, run the ``email-composer`` skill with the original message
    body as the brief + thread context. Store the result as an
    ObligationEvent so future ticks skip and the user can review/approve.
    """
    from sqlmodel import select

    from agent_core.skills import (
        LanguageModelError,
        SkillContext,
        SkillRunner,
        default_registry,
        language_model_from_settings,
    )
    from agent_core.state.models import (
        Obligation,
        ObligationEvent,
        ObligationEventKind,
        ObligationSource,
        ObligationStatus,
    )

    settings_obj = getattr(settings, "settings", settings)
    report = ComposeReport()

    if language_model is None:
        try:
            from agent_core.secrets import default_store

            language_model = language_model_from_settings(settings_obj, default_store())
        except LanguageModelError as e:
            report.errors.append(f"compose skipped: LLM not configured ({e})")
            return report

    with db.session() as s:
        candidates = list(
            s.exec(
                select(Obligation)
                .where(Obligation.source == ObligationSource.inbound_email)
                .where(Obligation.status == ObligationStatus.in_progress)
                .order_by(Obligation.created_at)
                .limit(limit * 3)
            ).all()
        )
        if not candidates:
            return report

        ob_ids = [ob.id for ob in candidates]
        events = list(
            s.exec(select(ObligationEvent).where(ObligationEvent.obligation_id.in_(ob_ids))).all()
        )

        triage_marked_draft: set[str] = set()
        already_drafted: set[str] = set()
        for ev in events:
            payload = ev.payload or {}
            if (
                ev.actor == "agent-triage"
                and ev.kind == ObligationEventKind.comment
                and payload.get("type") == "triage"
                and payload.get("action") == "draft"
            ):
                triage_marked_draft.add(ev.obligation_id)
            if ev.kind == ObligationEventKind.comment and payload.get("type") in ("draft", "sent"):
                already_drafted.add(ev.obligation_id)

    fresh = [
        ob for ob in candidates if ob.id in triage_marked_draft and ob.id not in already_drafted
    ]
    report.candidates = len(fresh) + len([o for o in candidates if o.id in already_drafted])
    report.skipped_already_drafted = len([o for o in candidates if o.id in already_drafted])

    if not fresh:
        return report

    runner = SkillRunner(default_registry)

    for ob in fresh[:limit]:
        sender_addr, _ = _split_email_title(ob.title)
        thread_history = ob.body or ""
        original_message_id = _extract_original_message_id(events, ob.id)

        ctx = SkillContext(
            settings=settings_obj,
            db=db,
            language_model=language_model,
        )
        outcome = runner.run(
            "email-composer",
            {
                "to": sender_addr,
                "subject_hint": _extract_subject(ob.title),
                "brief": (
                    f"Reply to the email from {sender_addr}. "
                    "Match their tone. Address their question or request directly."
                ),
                "thread_history": thread_history,
                "formality_hint": "auto",
            },
            ctx,
        )
        if not outcome.succeeded:
            report.errors.append(f"compose {ob.id[:8]}: skill failed: {outcome.error}")
            continue

        result = outcome.result
        body = result.output.body
        subject = result.output.subject

        with db.session() as s:
            s.add(
                ObligationEvent(
                    obligation_id=ob.id,
                    kind=ObligationEventKind.comment,
                    actor="agent-composer",
                    payload={
                        "type": "draft",
                        "to": sender_addr,
                        "subject": subject,
                        "body": body,
                        "in_reply_to": original_message_id,
                        "confidence": result.confidence,
                        "rationale": result.rationale,
                    },
                )
            )
            s.commit()
        report.drafted += 1

    return report


# ── Send orchestration ─────────────────────────────────────────────────────


def send_draft(
    *,
    db: Database,
    sender: EmailSender,
    obligation_id: str,
) -> SendReport:
    """Look up the latest draft for ``obligation_id`` and send it.

    Updates the obligation to status=done on success, with an
    ObligationEvent payload type=sent recording the SMTP Message-ID
    returned by the server. Idempotent: if already sent, returns
    ``already_sent`` without re-shipping.
    """
    from sqlmodel import select

    from agent_core.state.models import (
        Obligation,
        ObligationEvent,
        ObligationEventKind,
        ObligationStatus,
    )

    with db.session() as s:
        ob = s.get(Obligation, obligation_id)
        if ob is None:
            return SendReport(
                sent=False,
                reason="obligation_missing",
                obligation_id=obligation_id,
                error=f"no obligation {obligation_id}",
            )

        events = list(
            s.exec(
                select(ObligationEvent)
                .where(ObligationEvent.obligation_id == obligation_id)
                .order_by(ObligationEvent.occurred_at.desc())
            ).all()
        )

    draft_event = None
    for ev in events:
        payload = ev.payload or {}
        if ev.kind == ObligationEventKind.comment and payload.get("type") == "draft":
            draft_event = ev
            break

    already_sent = any(
        ev.kind == ObligationEventKind.comment and (ev.payload or {}).get("type") == "sent"
        for ev in events
    )
    if already_sent:
        return SendReport(
            sent=False,
            reason="already_sent",
            obligation_id=obligation_id,
            error="this draft was already sent (see prior 'sent' event)",
        )

    if draft_event is None:
        return SendReport(
            sent=False,
            reason="no_draft",
            obligation_id=obligation_id,
            error=(
                "no draft event found for this obligation. Compose one first "
                "via `dcos email compose <id>` or wait for the autonomous tick."
            ),
        )

    payload = draft_event.payload or {}
    to = payload.get("to") or ""
    subject = payload.get("subject") or "(no subject)"
    body = payload.get("body") or ""
    in_reply_to = payload.get("in_reply_to")

    try:
        smtp_message_id = sender.send(
            to=to,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
        )
    except Exception as e:
        logger.exception("SMTP send failed for obligation %s", obligation_id)
        return SendReport(
            sent=False,
            reason="smtp_failed",
            obligation_id=obligation_id,
            to=to,
            subject=subject,
            error=str(e),
        )

    now = datetime.now(UTC)
    with db.session() as s:
        ob = s.get(Obligation, obligation_id)
        prior_status = ob.status
        ob.status = ObligationStatus.done
        ob.completed_at = now
        s.add(ob)
        s.add(
            ObligationEvent(
                obligation_id=obligation_id,
                kind=ObligationEventKind.comment,
                actor="agent-sender",
                payload={
                    "type": "sent",
                    "to": to,
                    "subject": subject,
                    "smtp_message_id": smtp_message_id,
                    "in_reply_to": in_reply_to,
                },
            )
        )
        s.add(
            ObligationEvent(
                obligation_id=obligation_id,
                kind=ObligationEventKind.status_changed,
                actor="agent-sender",
                payload={
                    "from": prior_status.value,
                    "to": ObligationStatus.done.value,
                    "reason": "draft sent",
                },
            )
        )
        s.commit()

    return SendReport(
        sent=True,
        reason="sent",
        obligation_id=obligation_id,
        to=to,
        subject=subject,
    )


# ── Internal helpers ────────────────────────────────────────────────────────


def _split_email_title(title: str) -> tuple[str, str]:
    """Reverse the ``Email from <sender>: <subject>`` convention."""
    if not title.startswith("Email from "):
        return ("", title)
    rest = title[len("Email from ") :]
    if ":" not in rest:
        return (rest.strip(), "")
    sender, _, subject = rest.partition(":")
    return (sender.strip(), subject.strip())


def _extract_subject(title: str) -> str:
    _, subject = _split_email_title(title)
    return subject


def _extract_original_message_id(events: list, obligation_id: str) -> str | None:
    """Pull the original incoming Message-ID from the 'created' event payload.

    InboundCapture.capture_email writes payload.message_id at creation time.
    """
    for ev in events:
        if ev.obligation_id != obligation_id:
            continue
        payload = ev.payload or {}
        # Look at created events first
        if payload.get("kind") == "email":
            return payload.get("message_id")
    return None


__all__ = [
    "ComposeReport",
    "EmailSendError",
    "EmailSender",
    "SendReport",
    "compose_drafts",
    "send_draft",
]
