"""agent_core.openbrain — semantic memory + multi-source ingest.

Per L16: a first-class agent-core feature available to both products.
Skills call `openbrain.search(query)` to surface relevant past context;
ingest pipelines (Sprint 7c) populate Thought rows from Drive, Gmail,
GitHub, vault, etc.

Sprint 7b ships:
  - EmbeddingProvider Protocol + StubEmbeddingProvider (deterministic;
    tests + dev) + OllamaEmbeddingProvider (production default)
  - OpenBrainStore: capture / search / recent / dedup-by-fingerprint
  - MCP-tool functions matching Esby's openbrain_mcp.py surface

Vector storage is JSON-list-of-floats today (portable across SQLite +
Postgres); Python-side cosine similarity at query time. Native vector
backends (pgvector / sqlite-vec) come in a future sprint when scale demands.
"""

from agent_core.openbrain.embeddings import (
    EmbeddingProvider,
    OllamaEmbeddingProvider,
    StubEmbeddingProvider,
)
from agent_core.openbrain.mcp_tools import (
    capture_thought,
    openbrain_stats,
    recent_thoughts,
    search_thoughts,
)
from agent_core.openbrain.store import OpenBrainStore, SearchHit

__all__ = [
    "EmbeddingProvider",
    "OllamaEmbeddingProvider",
    "OpenBrainStore",
    "SearchHit",
    "StubEmbeddingProvider",
    "capture_thought",
    "openbrain_stats",
    "recent_thoughts",
    "search_thoughts",
]
