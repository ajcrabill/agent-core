# ikb-agent

Team intelligent knowledge base. Built on `agent-core`.

See the [main README](../../README.md) for the full picture.

## What it does

- Ingests knowledge from Drive, Gmail, GitHub, Notion, Beehiiv, vault, and more (via `agent-core.openbrain`)
- Provides semantic search across everything ingested, with source attribution + freshness tracking
- Drafts content (newsletters, evaluations, briefings) using supervised learning over exemplars
- Surfaces conflicts when sources disagree
- Runs the quality auditor always-on (accuracy is the whole point)
- Coordinates with `dcos-agent` instances natively via the mesh layer

## Default storage

PostgreSQL 16+ with pgvector. The Postgres dependency is required for ikb-agent because the semantic memory (`thoughts` table with vector embeddings) is the core feature.

## Access model (current)

Small trusted team — primary user + a handful of trusted collaborators sharing access. **Not** a true multi-tenant product. RBAC and per-user identity may come post-v1.
