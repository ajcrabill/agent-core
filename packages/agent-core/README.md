# agent-core

The platform library that `dcos-agent` and `ikb-agent` both depend on.

See the [main README](../../README.md) and [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) for the full picture.

## Submodules

| Module | Purpose | Sprint |
|---|---|---|
| `state` | identity, secrets, paths, db (sqlite\|postgres), schema migrations, vault renderer + watcher | 1 |
| `agent` | hermes adapter, persona loader, context-loader hooks, agent loop, skill registry, MCP wiring | 2, 2.5 |
| `work` | obligations, scheduler, cron watchdog, pipeline monitor, inbound capture, plan developer, completion verifier | 3 |
| `quality` | two-tier audit framework, score store, undelegation policy, weekly report | 4 |
| `actions` | class taxonomy, action log + rationale, daily digest synthesis | 4.5 |
| `learning` | store (jsonl + db), tag resolver, maintenance, capture detector, firing log | 5a, 5b |
| `content_creation` | exemplars, iterations, diff-extractor, calibration | 5c |
| `mesh` | agent-to-agent service: db-backed, ed25519 auth, /send /pending /recv /ack | 6 |
| `openbrain` | semantic memory + ingest pipelines (Drive, Gmail, GitHub, Notion, ...) | 7 |
| `ops` | doctor, backup (litestream/pg_dump), restore, 3-tier interview wizard | 8 |
