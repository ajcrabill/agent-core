"""agent_core.mesh — agent-to-agent collaboration layer.

Per L17 (collaboration layer is first-class default): both dcos-agent and
ikb-agent ship with mesh enabled. Two agents on the same Tailnet (or any
HTTP-reachable network) discover each other, exchange ed25519 keys, and
send signed messages with at-least-once delivery + explicit ack semantics.

Ports the team-mcp-server tool surface verbatim so existing MCP-using code
doesn't break:
  team_send_message / team_get_messages / team_get_thread /
  team_get_daily_digest / team_list_channels / team_search_messages

Sprint 6a (this commit): types, signer, peers, in-process transport,
high-level MeshClient, MCP-tool functions. All single-process / shared-db
scenarios are fully testable.

Sprint 6b: HTTP server + HTTP client transport for cross-machine
deployments (the actual dCoS-on-laptop ↔ iKB-on-server use case).
"""

from agent_core.mesh.client import MeshClient
from agent_core.mesh.http import HttpMeshServer, HttpTransport
from agent_core.mesh.mcp_tools import (
    team_get_daily_digest,
    team_get_messages,
    team_get_thread,
    team_list_peers,
    team_search_messages,
    team_send_message,
)
from agent_core.mesh.peers import PeerRegistry
from agent_core.mesh.signer import Ed25519Signer, NullSigner, Signer
from agent_core.mesh.transport import InProcessTransport, Transport
from agent_core.mesh.types import (
    DeliveryResult,
    MessageEnvelope,
    SignatureError,
)

__all__ = [
    # Types
    "DeliveryResult",
    "MessageEnvelope",
    "SignatureError",
    # Signer
    "Ed25519Signer",
    "NullSigner",
    "Signer",
    # Peers
    "PeerRegistry",
    # Transport
    "HttpMeshServer",
    "HttpTransport",
    "InProcessTransport",
    "Transport",
    # Client
    "MeshClient",
    # MCP tools
    "team_get_daily_digest",
    "team_get_messages",
    "team_get_thread",
    "team_list_peers",
    "team_search_messages",
    "team_send_message",
]
