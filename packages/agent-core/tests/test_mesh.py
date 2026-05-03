"""Sprint 6a — mesh layer tests (in-process)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from agent_core.mesh import (
    Ed25519Signer,
    InProcessTransport,
    MeshClient,
    MessageEnvelope,
    NullSigner,
    PeerRegistry,
    SignatureError,
    Signer,
    Transport,
    team_get_daily_digest,
    team_get_messages,
    team_get_thread,
    team_list_peers,
    team_search_messages,
    team_send_message,
)
from agent_core.state import (
    Database,
    Identity,
    IntercomMessage,
    IntercomState,
    PeerRole,
    utcnow,
)
from sqlmodel import select


def _new_db(instance_name: str) -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(Identity(instance_name=instance_name))
        s.commit()
    return db


# ── MessageEnvelope serialization ───────────────────────────────────────────


def test_envelope_roundtrip() -> None:
    e = MessageEnvelope(
        id="abc",
        sender="A",
        recipient="B",
        body="hi",
        payload={"channel": "ops"},
        ttl_seconds=3600,
        sent_at="2026-05-03T00:00:00+00:00",
        idempotency_key="k1",
        signature="sig",
    )
    d = e.to_dict()
    assert d["id"] == "abc"
    assert d["payload"] == {"channel": "ops"}
    e2 = MessageEnvelope.from_dict(d)
    assert e2.id == "abc"
    assert e2.payload == {"channel": "ops"}
    assert e2.idempotency_key == "k1"
    assert e2.signature == "sig"


def test_envelope_signed_payload_is_deterministic() -> None:
    """The bytes signed must be identical regardless of dict iteration order."""
    e1 = MessageEnvelope(id="x", sender="a", recipient="b", body="hi", sent_at="t")
    e2 = MessageEnvelope(id="x", sender="a", recipient="b", body="hi", sent_at="t")
    assert e1.signed_payload_bytes() == e2.signed_payload_bytes()


def test_envelope_signed_payload_excludes_signature() -> None:
    e = MessageEnvelope(id="x", sender="a", recipient="b", body="hi", sent_at="t")
    sb = e.signed_payload_bytes()
    e.signature = "after-the-fact"
    assert e.signed_payload_bytes() == sb  # signature must not be in signed bytes


# ── Ed25519Signer ───────────────────────────────────────────────────────────


def test_ed25519_generate_and_roundtrip_signature() -> None:
    s = Ed25519Signer.generate()
    e = MessageEnvelope(id="x", sender="A", recipient="B", body="hi", sent_at="t")
    sig = s.sign(e)
    assert isinstance(sig, str) and len(sig) > 0
    e.signature = sig
    s.verify(e, s.public_key_b64)  # no exception → ok


def test_ed25519_verify_rejects_tampered_envelope() -> None:
    s = Ed25519Signer.generate()
    e = MessageEnvelope(id="x", sender="A", recipient="B", body="hi", sent_at="t")
    e.signature = s.sign(e)
    e.body = "tampered"
    with pytest.raises(SignatureError):
        s.verify(e, s.public_key_b64)


def test_ed25519_verify_rejects_wrong_key() -> None:
    s1 = Ed25519Signer.generate()
    s2 = Ed25519Signer.generate()
    e = MessageEnvelope(id="x", sender="A", recipient="B", body="hi", sent_at="t")
    e.signature = s1.sign(e)
    with pytest.raises(SignatureError):
        s2.verify(e, s2.public_key_b64)  # wrong sender's pk


def test_ed25519_invalid_seed_length() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Ed25519Signer(b"too-short")


def test_ed25519_seed_roundtrip_via_b64() -> None:
    s1 = Ed25519Signer.generate()
    seed_b64 = s1.seed_b64
    s2 = Ed25519Signer(seed_b64)
    assert s2.public_key_b64 == s1.public_key_b64


def test_null_signer_accepts_anything() -> None:
    s = NullSigner()
    e = MessageEnvelope(id="x", sender="A", recipient="B", body="hi", sent_at="t")
    e.signature = s.sign(e)
    s.verify(e, "anything")  # no exception


def test_signer_protocol_satisfaction() -> None:
    assert isinstance(Ed25519Signer.generate(), Signer)
    assert isinstance(NullSigner(), Signer)


# ── PeerRegistry ────────────────────────────────────────────────────────────


def test_peer_add_idempotent_on_instance_name() -> None:
    db = _new_db("me")
    pr = PeerRegistry(db)
    pr.add(instance_name="ikb-bob", role=PeerRole.ikb, endpoint_url="http://a")
    pr.add(instance_name="ikb-bob", role=PeerRole.ikb, endpoint_url="http://b")
    peers = pr.list_all()
    assert len(peers) == 1
    assert peers[0].endpoint_url == "http://b"


def test_peer_remove() -> None:
    db = _new_db("me")
    pr = PeerRegistry(db)
    pr.add(instance_name="x")
    pr.remove("x")
    assert pr.get("x") is None


def test_peer_remove_unknown_raises() -> None:
    pr = PeerRegistry(_new_db("me"))
    with pytest.raises(ValueError, match="not found"):
        pr.remove("nope")


def test_peer_list_by_role() -> None:
    db = _new_db("me")
    pr = PeerRegistry(db)
    pr.add(instance_name="dcos-anna", role=PeerRole.dcos)
    pr.add(instance_name="ikb-bob", role=PeerRole.ikb)
    pr.add(instance_name="dcos-claude", role=PeerRole.dcos)
    dcos = pr.list_by_role(PeerRole.dcos)
    assert {p.instance_name for p in dcos} == {"dcos-anna", "dcos-claude"}


def test_peer_mark_seen_updates_timestamp() -> None:
    db = _new_db("me")
    pr = PeerRegistry(db)
    pr.add(instance_name="x")
    p = pr.mark_seen("x")
    assert p.last_seen_at is not None


# ── Transport: in-process routing ───────────────────────────────────────────


def _two_peer_setup():
    """Build two MeshClients (a/b) wired through one InProcessTransport."""
    db_a = _new_db("AgentA")
    db_b = _new_db("AgentB")
    sa = Ed25519Signer.generate()
    sb = Ed25519Signer.generate()

    # Each side knows about the other's public key
    pr_a = PeerRegistry(db_a)
    pr_a.add(instance_name="AgentB", role=PeerRole.other, public_key=sb.public_key_b64)
    pr_b = PeerRegistry(db_b)
    pr_b.add(instance_name="AgentA", role=PeerRole.other, public_key=sa.public_key_b64)

    transport = InProcessTransport()
    client_a = MeshClient(db_a, signer=sa, transport=transport, peers=pr_a)
    client_b = MeshClient(db_b, signer=sb, transport=transport, peers=pr_b)
    transport.register_receiver("AgentA", client_a.receive_envelope)
    transport.register_receiver("AgentB", client_b.receive_envelope)
    return client_a, client_b, db_a, db_b


def test_send_delivers_to_peer_inbox() -> None:
    a, b, db_a, db_b = _two_peer_setup()
    result = a.send(recipient="AgentB", body="hello bob")
    assert result.accepted
    assert not result.duplicated

    # B has it in unread
    inbox_b = b.unread()
    assert len(inbox_b) == 1
    assert inbox_b[0].body == "hello bob"
    assert inbox_b[0].sender == "AgentA"
    assert inbox_b[0].state == IntercomState.delivered


def test_send_unknown_recipient_returns_unaccepted() -> None:
    db_a = _new_db("AgentA")
    sa = Ed25519Signer.generate()
    transport = InProcessTransport()
    client_a = MeshClient(db_a, signer=sa, transport=transport)

    result = client_a.send(recipient="ghost", body="anyone there?")
    assert result.accepted is False
    assert "no receiver" in (result.reason or "")


def test_send_persists_outbound_even_when_undeliverable() -> None:
    db_a = _new_db("AgentA")
    transport = InProcessTransport()
    client_a = MeshClient(db_a, signer=NullSigner(), transport=transport)

    client_a.send(recipient="ghost", body="nobody home")
    with db_a.session() as s:
        rows = list(s.exec(select(IntercomMessage)).all())
    assert len(rows) == 1
    assert rows[0].state == IntercomState.pending  # never advanced to delivered


def test_receive_dedups_on_id() -> None:
    a, b, db_a, db_b = _two_peer_setup()
    result1 = a.send(recipient="AgentB", body="x")
    # Replay via direct receive_envelope call with the same envelope id
    env = MessageEnvelope(
        id=result1.message_id,
        sender="AgentA",
        recipient="AgentB",
        body="x",
        sent_at=utcnow().isoformat(),
    )
    env.signature = a.signer.sign(env)
    result2 = b.receive_envelope(env)
    assert result2.accepted
    assert result2.duplicated
    # Still only one row
    with db_b.session() as s:
        assert len(list(s.exec(select(IntercomMessage)).all())) == 1


def test_receive_rejects_wrong_recipient() -> None:
    a, b, _, _ = _two_peer_setup()
    env = MessageEnvelope(id="x", sender="AgentA", recipient="SomeoneElse", body="hi", sent_at="t")
    env.signature = a.signer.sign(env)
    result = b.receive_envelope(env)
    assert result.accepted is False
    assert "is not me" in (result.reason or "")


def test_receive_rejects_bad_signature_when_pubkey_known() -> None:
    a, b, _, _ = _two_peer_setup()
    env = MessageEnvelope(id="x", sender="AgentA", recipient="AgentB", body="hi", sent_at="t")
    env.signature = (
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    result = b.receive_envelope(env)
    assert result.accepted is False
    assert "signature" in (result.reason or "").lower()


def test_receive_marks_peer_last_seen() -> None:
    a, b, _, db_b = _two_peer_setup()
    a.send(recipient="AgentB", body="hi")
    assert b.peers.get("AgentA").last_seen_at is not None


# ── Ack flow ────────────────────────────────────────────────────────────────


def test_ack_marks_message_acknowledged() -> None:
    a, b, _, db_b = _two_peer_setup()
    result = a.send(recipient="AgentB", body="ack me")
    msg_id = result.message_id

    b.ack(msg_id, note="processed")
    with db_b.session() as s:
        msg = s.get(IntercomMessage, msg_id)
    assert msg.state == IntercomState.acknowledged
    assert msg.acknowledged_at is not None


def test_ack_unknown_id_raises() -> None:
    db = _new_db("me")
    client = MeshClient(db, signer=NullSigner(), transport=InProcessTransport())
    with pytest.raises(ValueError, match="not found"):
        client.ack("never-existed")


def test_ack_message_not_addressed_to_us_raises() -> None:
    db = _new_db("me")
    # Insert a message addressed to someone else
    with db.session() as s:
        s.add(
            IntercomMessage(id="x", sender="other", recipient="not-me", body="hi", sent_at=utcnow())
        )
        s.commit()
    client = MeshClient(db, signer=NullSigner(), transport=InProcessTransport())
    with pytest.raises(ValueError, match="not addressed to us"):
        client.ack("x")


# ── Reading: thread + unread ────────────────────────────────────────────────


def test_thread_returns_bidirectional_history() -> None:
    a, b, _, _ = _two_peer_setup()
    a.send(recipient="AgentB", body="msg 1")
    b.send(recipient="AgentA", body="reply 1")
    a.send(recipient="AgentB", body="msg 2")
    thread_for_a = a.thread("AgentB")
    bodies = [m.body for m in thread_for_a]
    assert bodies == ["msg 1", "reply 1", "msg 2"]


def test_unread_filters_out_acknowledged() -> None:
    a, b, _, _ = _two_peer_setup()
    r1 = a.send(recipient="AgentB", body="one")
    r2 = a.send(recipient="AgentB", body="two")
    b.ack(r1.message_id)
    unread = b.unread()
    assert {m.id for m in unread} == {r2.message_id}


# ── MCP-tool functions ──────────────────────────────────────────────────────


def test_team_send_message_returns_dict() -> None:
    a, b, _, _ = _two_peer_setup()
    result = team_send_message(a, recipient="AgentB", body="hi")
    assert result["accepted"] is True
    assert result["message_id"]


def test_team_get_messages_inbox_only() -> None:
    a, b, _, _ = _two_peer_setup()
    a.send(recipient="AgentB", body="m1")
    a.send(recipient="AgentB", body="m2")
    b.send(recipient="AgentA", body="reply")
    inbox_b = team_get_messages(b, include_outbound=False)
    assert all(m["recipient"] == "AgentB" for m in inbox_b)
    assert len(inbox_b) == 2


def test_team_get_messages_search_filters() -> None:
    a, b, _, _ = _two_peer_setup()
    a.send(recipient="AgentB", body="apple in the body")
    a.send(recipient="AgentB", body="banana in the body")
    matches = team_get_messages(b, search="apple", include_outbound=False)
    assert len(matches) == 1
    assert "apple" in matches[0]["body"]


def test_team_get_thread_uses_client() -> None:
    a, b, _, _ = _two_peer_setup()
    a.send(recipient="AgentB", body="hello")
    thread = team_get_thread(b, peer_instance_name="AgentA")
    assert len(thread) == 1
    assert thread[0]["body"] == "hello"


def test_team_search_messages_aliases_get_messages() -> None:
    a, b, _, _ = _two_peer_setup()
    a.send(recipient="AgentB", body="needle in haystack")
    a.send(recipient="AgentB", body="other")
    found = team_search_messages(b, query="needle")
    assert len(found) == 1


def test_team_get_daily_digest_counts() -> None:
    a, b, _, _ = _two_peer_setup()
    a.send(recipient="AgentB", body="m1")
    a.send(recipient="AgentB", body="m2")
    b.send(recipient="AgentA", body="reply")

    digest = team_get_daily_digest(b)
    assert digest["total"] == 3
    assert digest["inbound"] == 2
    assert digest["outbound"] == 1
    assert digest["unread_inbound"] == 2  # both inbound, not yet ack'd
    assert digest["by_peer"]["AgentA"] == 3


def test_team_get_daily_digest_excludes_outside_window() -> None:
    a, b, _, db_b = _two_peer_setup()
    a.send(recipient="AgentB", body="recent")
    # Backdate one inbound
    with db_b.session() as s:
        msg = list(s.exec(select(IntercomMessage)).all())[0]
        msg.sent_at = utcnow() - timedelta(days=30)
        s.add(msg)
        s.commit()
    digest = team_get_daily_digest(b, period_hours=24)
    assert digest["total"] == 0


def test_team_list_peers_filters_by_role() -> None:
    db = _new_db("me")
    pr = PeerRegistry(db)
    pr.add(instance_name="dcos-x", role=PeerRole.dcos)
    pr.add(instance_name="ikb-y", role=PeerRole.ikb)
    client = MeshClient(db, signer=NullSigner(), transport=InProcessTransport(), peers=pr)
    out = team_list_peers(client, role=PeerRole.ikb)
    assert len(out) == 1
    assert out[0]["instance_name"] == "ikb-y"


def test_team_list_peers_returns_metadata() -> None:
    db = _new_db("me")
    pr = PeerRegistry(db)
    pr.add(instance_name="ikb-y", role=PeerRole.ikb, endpoint_url="http://x", public_key="abc")
    client = MeshClient(db, signer=NullSigner(), transport=InProcessTransport(), peers=pr)
    out = team_list_peers(client)
    assert out[0]["has_public_key"] is True
    assert out[0]["endpoint_url"] == "http://x"


# ── End-to-end realistic scenario ───────────────────────────────────────────


def test_e2e_dcos_to_ikb_roundtrip() -> None:
    """Realistic: dCoS sends iKB a question; iKB receives, processes, acks;
    dCoS sees the ack via state change on its sent message."""
    a, b, db_a, db_b = _two_peer_setup()
    # Pretend AgentA is dCoS and AgentB is iKB
    out = team_send_message(a, recipient="AgentB", body="What did Q3 metrics say?")
    msg_id = out["message_id"]

    # iKB sees it in inbox
    inbox = team_get_messages(b, include_outbound=False)
    assert len(inbox) == 1
    assert inbox[0]["body"].startswith("What did Q3")

    # iKB processes + acks
    b.ack(msg_id, note="answered in chat")

    # iKB now sees zero unread
    digest = team_get_daily_digest(b)
    assert digest["unread_inbound"] == 0

    # dCoS sees the message in its sent items
    sent = team_get_messages(a, include_outbound=True)
    sent_only = [m for m in sent if m["sender"] == "AgentA"]
    assert len(sent_only) == 1


# ── Transport protocol satisfaction ─────────────────────────────────────────


def test_transport_protocol_satisfaction() -> None:
    assert isinstance(InProcessTransport(), Transport)
