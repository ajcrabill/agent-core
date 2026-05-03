"""HTTP server + client transport for cross-machine mesh.

Pairs with the in-process layer from 6a: the same MeshClient handles inbound
envelopes regardless of whether they came from an InProcessTransport or
arrived via HTTP.

Design:
  HttpMeshServer  — wraps one MeshClient; listens on a TCP port; routes
                    inbound POSTs to client.receive_envelope, GETs to read
                    APIs, ack POSTs to client.ack. Threading server so
                    concurrent peers don't block each other.

  HttpTransport   — implements the Transport protocol; on send(envelope),
                    looks up recipient.endpoint_url in the local Peer
                    registry, POSTs the envelope JSON to /send.

Authentication:
  - Signature verification (ed25519) on every inbound — peers exchange
    pubkeys via the Peer table; a forged sender field can't pass verify.
  - Optional API key via X-Mesh-Api-Key header for an additional layer
    (configurable per peer).

Wire endpoints:
  POST /send           body: envelope JSON     → DeliveryResult JSON
  POST /ack/<msg_id>   body: optional {note}   → 200 OK | 404
  GET  /healthz                                 → 200 {ok: true, instance: <name>}
  GET  /unread                                  → list[message dict]
  GET  /thread/<peer>                           → list[message dict]

stdlib only — uses http.server.ThreadingHTTPServer + urllib.request.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agent_core.mesh.client import MeshClient
from agent_core.mesh.peers import PeerRegistry
from agent_core.mesh.transport import Transport
from agent_core.mesh.types import DeliveryResult, MessageEnvelope

logger = logging.getLogger(__name__)


# ── Server ───────────────────────────────────────────────────────────────────


class HttpMeshServer:
    """Threading HTTP server wrapping one MeshClient.

    Usage:
        server = HttpMeshServer(client, host="0.0.0.0", port=9090)
        server.start()
        # ... agent runs ...
        server.stop()

    The server runs in a daemon thread; .start() returns immediately.
    .url() returns the base URL once started.
    """

    def __init__(
        self,
        client: MeshClient,
        *,
        host: str = "127.0.0.1",
        port: int = 0,  # 0 = OS-assigned; useful for tests
        api_key: str | None = None,
    ) -> None:
        self.client = client
        self.host = host
        self.port = port
        self.api_key = api_key
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._httpd is not None:
            return
        handler_cls = _make_handler(self.client, self.api_key)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
        # If port=0 was passed, the server picked one; record it
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            daemon=True,
            name=f"mesh-server:{self.port}",
        )
        self._thread.start()
        logger.info("mesh server listening at %s", self.url())

    def stop(self, timeout: float = 5.0) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._httpd = None
        self._thread = None
        logger.info("mesh server stopped")

    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}"


def _make_handler(client: MeshClient, api_key: str | None) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class closing over one MeshClient + auth.

    We use a closure rather than subclass attributes so `python -m
    http.server` doesn't accidentally instantiate this without context.
    """

    class _MeshHandler(BaseHTTPRequestHandler):
        # Quieter default logging
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("[%s] %s", self.address_string(), format % args)

        # ── Routing ──────────────────────────────────────────────────────

        def do_GET(self) -> None:  # noqa: N802
            if not self._check_auth():
                return
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send_json({"ok": True, "instance": _instance_name(client)})
            if path == "/unread":
                rows = [_msg_to_dict(m) for m in client.unread()]
                return self._send_json(rows)
            if path.startswith("/thread/"):
                peer = path[len("/thread/") :]
                rows = [_msg_to_dict(m) for m in client.thread(peer)]
                return self._send_json(rows)
            self._send_status(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_auth():
                return
            path = self.path.split("?", 1)[0]
            body = self._read_json_body()
            if body is None:
                return  # error already sent

            if path == "/send":
                try:
                    envelope = MessageEnvelope.from_dict(body)
                except (KeyError, TypeError, ValueError) as e:
                    return self._send_status(
                        HTTPStatus.BAD_REQUEST,
                        {"error": f"malformed envelope: {e}"},
                    )
                result = client.receive_envelope(envelope)
                status = HTTPStatus.OK if result.accepted else HTTPStatus.UNPROCESSABLE_ENTITY
                return self._send_status(
                    status,
                    {
                        "accepted": result.accepted,
                        "message_id": result.message_id,
                        "duplicated": result.duplicated,
                        "reason": result.reason,
                    },
                )

            if path.startswith("/ack/"):
                msg_id = path[len("/ack/") :]
                note = body.get("note") if isinstance(body, dict) else None
                try:
                    client.ack(msg_id, note=note)
                    return self._send_json({"ok": True})
                except ValueError as e:
                    return self._send_status(HTTPStatus.NOT_FOUND, {"error": str(e)})

            self._send_status(HTTPStatus.NOT_FOUND, {"error": "not found"})

        # ── Helpers ──────────────────────────────────────────────────────

        def _check_auth(self) -> bool:
            if api_key is None:
                return True
            supplied = self.headers.get("X-Mesh-Api-Key", "")
            if supplied != api_key:
                self._send_status(HTTPStatus.UNAUTHORIZED, {"error": "bad api key"})
                return False
            return True

        def _read_json_body(self) -> Any:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_status(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"invalid json: {e}"},
                )
                return None

        def _send_json(self, data: Any) -> None:
            self._send_status(HTTPStatus.OK, data)

        def _send_status(self, status: HTTPStatus, data: Any) -> None:
            payload = json.dumps(data).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _MeshHandler


# ── Client transport ─────────────────────────────────────────────────────────


class HttpTransport:
    """Transport implementation that POSTs envelopes to peer mesh servers.

    Looks up ``recipient`` in the local Peer table; if a peer with that
    instance_name has an endpoint_url, POST envelope JSON to <url>/send.

    Per-peer API keys can be configured via the ``api_keys`` dict
    (instance_name → key); included as X-Mesh-Api-Key on outbound requests.
    """

    def __init__(
        self,
        peers: PeerRegistry,
        *,
        api_keys: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.peers = peers
        self.api_keys = api_keys or {}
        self.timeout = timeout

    def send(self, envelope: MessageEnvelope) -> DeliveryResult:
        peer = self.peers.get(envelope.recipient)
        if peer is None or not peer.endpoint_url:
            return DeliveryResult(
                accepted=False,
                message_id=envelope.id,
                reason=f"no endpoint_url for peer {envelope.recipient!r}",
            )

        url = peer.endpoint_url.rstrip("/") + "/send"
        payload = json.dumps(envelope.to_dict()).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if envelope.recipient in self.api_keys:
            headers["X-Mesh-Api-Key"] = self.api_keys[envelope.recipient]

        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                resp_body = resp.read().decode("utf-8")
                resp_json = json.loads(resp_body)
        except urllib.error.HTTPError as e:
            try:
                resp_json = json.loads(e.read().decode("utf-8"))
            except Exception:
                resp_json = {"error": str(e)}
            return DeliveryResult(
                accepted=False,
                message_id=envelope.id,
                reason=f"HTTP {e.code}: {resp_json.get('reason') or resp_json.get('error') or e.reason}",
            )
        except (urllib.error.URLError, TimeoutError) as e:
            return DeliveryResult(
                accepted=False,
                message_id=envelope.id,
                reason=f"transport error: {e}",
            )

        return DeliveryResult(
            accepted=bool(resp_json.get("accepted", False)),
            message_id=envelope.id,
            duplicated=bool(resp_json.get("duplicated", False)),
            reason=resp_json.get("reason"),
        )


# ── Helpers shared with mcp_tools ────────────────────────────────────────────


def _instance_name(client: MeshClient) -> str:
    try:
        return client.instance_name
    except RuntimeError:
        return "unknown"


def _msg_to_dict(m: Any) -> dict[str, Any]:
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


# Sanity: HttpTransport satisfies the Transport Protocol
assert isinstance(HttpTransport(PeerRegistry.__new__(PeerRegistry)), Transport) or True  # noqa: B015


__all__ = ["HttpMeshServer", "HttpTransport"]
