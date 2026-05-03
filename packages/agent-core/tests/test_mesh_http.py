"""Sprint 6b — mesh HTTP server + transport tests.

Spins up a real server on an OS-assigned port, exercises the wire protocol
end-to-end. Covers:
  - send via HTTP delivers to recipient's MeshClient
  - signature verification works over the wire
  - idempotency on envelope.id (replay returns duplicated=True)
  - ack via HTTP marks message acknowledged
  - GET /healthz, /unread, /thread
  - api-key auth (when configured)
  - bad-recipient / bad-signature / bad-payload error paths
  - HttpTransport gracefully handles unreachable peers
"""

from __future__ import annotations

import json
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from agent_core.mesh import (
    Ed25519Signer,
    HttpMeshServer,
    HttpTransport,
    MeshClient,
    NullSigner,
    PeerRegistry,
)
from agent_core.state import (
    Database,
    Identity,
    PeerRole,
)


def _new_db(instance_name: str) -> Database:
    """File-backed SQLite (NOT :memory:) — the HTTP server runs handlers on
    a worker thread and :memory: dbs are per-connection. Production always
    uses file-backed sqlite, so this mirrors real usage."""
    fd, path = tempfile.mkstemp(prefix=f"mesh-test-{instance_name}-", suffix=".db")
    Path(path).unlink()  # let Database create it
    import os

    os.close(fd)
    db = Database.sqlite(path)
    db.create_all()
    with db.session() as s:
        s.add(Identity(instance_name=instance_name))
        s.commit()
    return db


def _make_pair_with_http(*, server_api_key: str | None = None, share_keys: bool = True):
    """Build (sender, server, server_url) with the recipient's MeshClient
    behind an HttpMeshServer. Sender's Peer registry knows the recipient's
    endpoint_url so HttpTransport can route to it.
    """
    db_recipient = _new_db("Recipient")
    db_sender = _new_db("Sender")

    sk_recipient = Ed25519Signer.generate()
    sk_sender = Ed25519Signer.generate()

    # Recipient's peer registry knows about sender's pubkey (so it can verify)
    pr_recipient = PeerRegistry(db_recipient)
    pr_recipient.add(
        instance_name="Sender",
        role=PeerRole.other,
        public_key=sk_sender.public_key_b64 if share_keys else None,
    )

    recipient_client = MeshClient(
        db_recipient,
        signer=sk_recipient,
        transport=None,  # server doesn't need a transport for receiving
        peers=pr_recipient,
    )
    server = HttpMeshServer(recipient_client, port=0, api_key=server_api_key)
    server.start()

    # Sender's peer registry has the server's endpoint_url
    pr_sender = PeerRegistry(db_sender)
    pr_sender.add(
        instance_name="Recipient",
        role=PeerRole.other,
        endpoint_url=server.url(),
        public_key=sk_recipient.public_key_b64 if share_keys else None,
    )

    api_keys = {"Recipient": server_api_key} if server_api_key else {}
    sender_transport = HttpTransport(pr_sender, api_keys=api_keys)
    sender_client = MeshClient(
        db_sender,
        signer=sk_sender,
        transport=sender_transport,
        peers=pr_sender,
    )

    return sender_client, recipient_client, server, db_sender, db_recipient


# ── healthz ─────────────────────────────────────────────────────────────────


def test_healthz_responds() -> None:
    _, _, server, _, _ = _make_pair_with_http()
    try:
        with urllib.request.urlopen(server.url() + "/healthz", timeout=2) as r:
            data = json.loads(r.read())
        assert data["ok"] is True
        assert data["instance"] == "Recipient"
    finally:
        server.stop()


# ── send over HTTP ──────────────────────────────────────────────────────────


def test_send_over_http_delivers_to_recipient() -> None:
    sender, recipient, server, _, _ = _make_pair_with_http()
    try:
        result = sender.send(recipient="Recipient", body="hi over http")
        assert result.accepted is True
        assert not result.duplicated

        # Recipient sees it
        unread = recipient.unread()
        assert len(unread) == 1
        assert unread[0].body == "hi over http"
        assert unread[0].sender == "Sender"
    finally:
        server.stop()


def test_send_over_http_idempotent_on_envelope_id() -> None:
    """Replay of the same envelope id returns duplicated=True, no second insert.

    Crafts the envelope directly + signs once + POSTs twice (the realistic
    "network delivered the same bytes twice" scenario). Reconstructing from
    the DB row would lose datetime precision and break signature verify.
    """
    sender, recipient, server, _, db_recipient = _make_pair_with_http()
    try:
        from agent_core.mesh import MessageEnvelope
        from agent_core.state import IntercomMessage
        from sqlmodel import select

        env = MessageEnvelope(
            id="dup-test-id",
            sender="Sender",
            recipient="Recipient",
            body="dup test",
            sent_at="2026-05-03T12:00:00+00:00",
        )
        env.signature = sender.signer.sign(env)

        # First POST
        req1 = urllib.request.Request(
            server.url() + "/send",
            data=json.dumps(env.to_dict()).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req1, timeout=2) as r:
            resp1 = json.loads(r.read())
        assert resp1["accepted"] is True
        assert resp1["duplicated"] is False

        # Second POST — same bytes, same signature
        req2 = urllib.request.Request(
            server.url() + "/send",
            data=json.dumps(env.to_dict()).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=2) as r:
            resp2 = json.loads(r.read())
        assert resp2["accepted"] is True
        assert resp2["duplicated"] is True

        # Still only one row in recipient db
        with db_recipient.session() as s:
            rows = list(
                s.exec(select(IntercomMessage).where(IntercomMessage.id == "dup-test-id")).all()
            )
        assert len(rows) == 1
    finally:
        server.stop()


def test_signature_verification_over_http() -> None:
    """End-to-end: tampered body in transit fails verify on receiver."""
    sender, recipient, server, _, _ = _make_pair_with_http()
    try:
        from agent_core.mesh import MessageEnvelope

        env = MessageEnvelope(
            id="test-tampered",
            sender="Sender",
            recipient="Recipient",
            body="original",
            sent_at="2026-05-03T00:00:00+00:00",
        )
        env.signature = sender.signer.sign(env)
        # Tamper after signing
        env.body = "tampered"

        req = urllib.request.Request(
            server.url() + "/send",
            data=json.dumps(env.to_dict()).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2)
        assert exc_info.value.code == 422
        body = json.loads(exc_info.value.read())
        assert body["accepted"] is False
        assert "signature" in (body["reason"] or "").lower()
    finally:
        server.stop()


def test_send_returns_unaccepted_when_recipient_unknown() -> None:
    sender, _, server, _, _ = _make_pair_with_http()
    try:
        result = sender.send(recipient="NonExistent", body="hi")
        assert result.accepted is False
        assert "no endpoint_url" in (result.reason or "")
    finally:
        server.stop()


def test_http_transport_handles_unreachable_peer() -> None:
    """If the peer's endpoint is down, send returns unaccepted (not raises)."""
    db = _new_db("Sender")
    pr = PeerRegistry(db)
    pr.add(
        instance_name="GhostPeer",
        endpoint_url="http://127.0.0.1:1",  # almost-certainly-closed port
    )
    transport = HttpTransport(pr, timeout=1.0)
    client = MeshClient(db, signer=NullSigner(), transport=transport, peers=pr)
    result = client.send(recipient="GhostPeer", body="anyone there?")
    assert result.accepted is False
    assert "transport error" in (result.reason or "") or "HTTP" in (result.reason or "")


# ── Ack over HTTP ───────────────────────────────────────────────────────────


def test_ack_via_http_marks_acknowledged() -> None:
    sender, recipient, server, _, db_recipient = _make_pair_with_http()
    try:
        result = sender.send(recipient="Recipient", body="ack me")
        msg_id = result.message_id

        req = urllib.request.Request(
            server.url() + f"/ack/{msg_id}",
            data=json.dumps({"note": "processed via HTTP"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200

        # Verify on recipient
        from agent_core.state import IntercomMessage, IntercomState

        with db_recipient.session() as s:
            msg = s.get(IntercomMessage, msg_id)
        assert msg.state == IntercomState.acknowledged
    finally:
        server.stop()


def test_ack_unknown_id_returns_404() -> None:
    sender, recipient, server, _, _ = _make_pair_with_http()
    try:
        req = urllib.request.Request(
            server.url() + "/ack/never-existed",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2)
        assert exc_info.value.code == 404
    finally:
        server.stop()


# ── Read endpoints ──────────────────────────────────────────────────────────


def test_unread_endpoint_returns_inbox() -> None:
    sender, recipient, server, _, _ = _make_pair_with_http()
    try:
        sender.send(recipient="Recipient", body="m1")
        sender.send(recipient="Recipient", body="m2")
        with urllib.request.urlopen(server.url() + "/unread", timeout=2) as r:
            data = json.loads(r.read())
        bodies = sorted(m["body"] for m in data)
        assert bodies == ["m1", "m2"]
    finally:
        server.stop()


def test_thread_endpoint_returns_history() -> None:
    sender, recipient, server, _, _ = _make_pair_with_http()
    try:
        sender.send(recipient="Recipient", body="hello")
        with urllib.request.urlopen(server.url() + "/thread/Sender", timeout=2) as r:
            data = json.loads(r.read())
        assert len(data) == 1
        assert data[0]["body"] == "hello"
    finally:
        server.stop()


# ── API key auth ────────────────────────────────────────────────────────────


def test_api_key_required_when_configured() -> None:
    sender, recipient, server, _, _ = _make_pair_with_http(server_api_key="secret-key")
    try:
        # With matching key (passed via HttpTransport.api_keys), send works
        result = sender.send(recipient="Recipient", body="authorized")
        assert result.accepted is True
    finally:
        server.stop()


def test_api_key_mismatch_returns_401() -> None:
    sender, recipient, server, _, _ = _make_pair_with_http(server_api_key="secret-key")
    try:
        # Direct request with wrong key
        req = urllib.request.Request(
            server.url() + "/healthz",
            headers={"X-Mesh-Api-Key": "wrong"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2)
        assert exc_info.value.code == 401
    finally:
        server.stop()


def test_api_key_missing_returns_401() -> None:
    sender, recipient, server, _, _ = _make_pair_with_http(server_api_key="secret-key")
    try:
        req = urllib.request.Request(server.url() + "/healthz")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2)
        assert exc_info.value.code == 401
    finally:
        server.stop()


# ── Error paths ─────────────────────────────────────────────────────────────


def test_send_malformed_envelope_returns_400() -> None:
    _, _, server, _, _ = _make_pair_with_http()
    try:
        req = urllib.request.Request(
            server.url() + "/send",
            data=b'{"missing": "required fields"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2)
        assert exc_info.value.code == 400
        body = json.loads(exc_info.value.read())
        assert "malformed" in body["error"].lower()
    finally:
        server.stop()


def test_invalid_json_body_returns_400() -> None:
    _, _, server, _, _ = _make_pair_with_http()
    try:
        req = urllib.request.Request(
            server.url() + "/send",
            data=b"not json at all",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2)
        assert exc_info.value.code == 400
    finally:
        server.stop()


def test_unknown_path_returns_404() -> None:
    _, _, server, _, _ = _make_pair_with_http()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(server.url() + "/nope", timeout=2)
        assert exc_info.value.code == 404
    finally:
        server.stop()


# ── Lifecycle ───────────────────────────────────────────────────────────────


def test_server_start_stop_idempotent() -> None:
    db = _new_db("X")
    client = MeshClient(db, signer=NullSigner(), transport=None)
    server = HttpMeshServer(client, port=0)
    server.start()
    server.start()  # second call: no-op
    server.stop()
    server.stop()  # no-op


def test_server_picks_random_port_when_zero() -> None:
    db = _new_db("X")
    client = MeshClient(db, signer=NullSigner(), transport=None)
    server = HttpMeshServer(client, port=0)
    server.start()
    try:
        assert server.port > 0
        assert server.port != 0
    finally:
        server.stop()


# ── End-to-end: dCoS↔iKB cross-process ─────────────────────────────────────


def test_e2e_two_servers_can_talk_to_each_other() -> None:
    """Both agents stand up servers; both have HttpTransport; full
    bidirectional flow over the wire."""
    db_dcos = _new_db("dcos")
    db_ikb = _new_db("ikb")
    sk_dcos = Ed25519Signer.generate()
    sk_ikb = Ed25519Signer.generate()

    pr_dcos = PeerRegistry(db_dcos)
    pr_ikb = PeerRegistry(db_ikb)

    # Each side knows the other's pubkey (will set endpoint_url after
    # servers start)
    pr_dcos.add(instance_name="ikb", public_key=sk_ikb.public_key_b64)
    pr_ikb.add(instance_name="dcos", public_key=sk_dcos.public_key_b64)

    # Both clients (transports filled in once endpoints known)
    transport_dcos = HttpTransport(pr_dcos)
    transport_ikb = HttpTransport(pr_ikb)
    client_dcos = MeshClient(db_dcos, signer=sk_dcos, transport=transport_dcos, peers=pr_dcos)
    client_ikb = MeshClient(db_ikb, signer=sk_ikb, transport=transport_ikb, peers=pr_ikb)

    server_dcos = HttpMeshServer(client_dcos, port=0)
    server_ikb = HttpMeshServer(client_ikb, port=0)
    server_dcos.start()
    server_ikb.start()

    # Update endpoint URLs in both registries now that servers picked ports
    pr_dcos.add(
        instance_name="ikb",
        public_key=sk_ikb.public_key_b64,
        endpoint_url=server_ikb.url(),
    )
    pr_ikb.add(
        instance_name="dcos",
        public_key=sk_dcos.public_key_b64,
        endpoint_url=server_dcos.url(),
    )

    try:
        r1 = client_dcos.send(recipient="ikb", body="Q3 metrics?")
        assert r1.accepted
        # iKB sees it
        assert any(m.body == "Q3 metrics?" for m in client_ikb.unread())

        # iKB replies
        r2 = client_ikb.send(recipient="dcos", body="here you go")
        assert r2.accepted
        # dCoS sees the reply
        time.sleep(0.05)
        assert any(m.body == "here you go" for m in client_dcos.unread())

        # dCoS acks the reply
        client_dcos.ack(r2.message_id)
        digest = client_dcos.unread()
        assert all(m.body != "here you go" for m in digest)
    finally:
        server_dcos.stop()
        server_ikb.stop()
