# agent-core

Open-source platform for personal and team AI agents — `dcos-agent` (digital chief of staff) and `ikb-agent` (intelligent knowledge base) on a shared `agent-core`.

**Status**: pre-release. Active development. APIs and schema will change.

> This repository replaces the [legacy v1 dCoS](https://github.com/ajcrabill/dCoS/tree/legacy-v1) implementation. The v1 code is preserved on the `legacy-v1` branch; the new architecture starts here on `main`.

## What you get

- **`dcos-agent`** — single-user AI chief of staff. Email triage, calendar, obligations, content drafting, supervised learning of your preferences. Default backend: SQLite.
- **`ikb-agent`** — small-team intelligent knowledge base. Semantic search across ingested sources, document creation pipelines, two-tier quality auditor. Default backend: PostgreSQL + pgvector.
- **`agent-core`** — the platform both depend on. State, learning, mesh, quality, action policy, content-creation primitives, scheduler, watchdog.
- Native **agent-to-agent collaboration** via the mesh layer. dCoS and iKB instances find each other and coordinate out of the box.

## Design principles

1. **Goal-directed operation.** Every inbound (email, chat, peer message) spawns an obligation. Every obligation has testable completion criteria. Every autonomous action traces back to an obligation. Nothing is freelance.
2. **Bias for action with a safety net.** Agents discover, plan, execute, and report after the fact. The quality auditor scores delivered work; bad outputs cause auto-undelegation.
3. **State lives in code, not in instructions.** Learning rules, obligations, peer messages get *injected into the model's context by code*, not by "remember to read this" rules. This is the difference between an agent that works and one that drifts.
4. **Bring your own inference.** Local Ollama, Anthropic, OpenAI, DeepSeek, or any OpenAI-compatible endpoint. No vendor lock.
5. **Best-in-class tools at the edges.** Use OpenWebUI for chat, Obsidian / MkDocs for vault, ObligationBoard (built-in) for tasks. Don't reinvent UIs.

## Architecture (one-page)

```
                        ┌──────────────────┐
                        │   OpenWebUI      │ ← chat (skinned, branded)
                        └────────┬─────────┘
                                 │ OpenAI-compat API
┌────────────────────────────────┴───────────────────────────────┐
│                          agent-core                              │
│  state · learning · work · agent · quality · mesh · openbrain   │
│  · content-creation · actions · ops · identity · secrets        │
└──────────┬──────────────────────────────────────┬──────────────┘
           │                                      │
   ┌───────▼──────────┐                  ┌────────▼─────────┐
   │   dcos-agent     │                  │    ikb-agent     │
   │ (personal CoS)   │                  │ (team KB)        │
   └──────────────────┘                  └──────────────────┘
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full plan.

## Roadmap

This is a multi-sprint build. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Quickstart

The interactive setup wizard is the primary install path. Once published:

```bash
pipx install dcos-agent       # personal
pipx install ikb-agent        # team
agent-core init               # 3-tier interview wizard, 5-60 min
```

For now, see [docs/QUICKSTART.md](docs/QUICKSTART.md) for current state.

## Repository layout

```
agent-core/
├── packages/
│   ├── agent-core/          # the platform library
│   ├── dcos-agent/          # personal-CoS package
│   ├── ikb-agent/           # team-KB package
│   └── hermes/              # fork of NousResearch/hermes-agent (the runtime)
├── docs/
├── examples/
├── templates/               # default vault, default skills, default rule packs
└── scripts/
```

## Built on

- [Hermes](https://github.com/NousResearch/hermes-agent) — agent runtime (forked here)
- [OpenWebUI](https://github.com/open-webui/open-webui) — chat surface
- [Obsidian](https://obsidian.md) — vault editing (dCoS)
- [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) — vault publishing (iKB)
- PostgreSQL + [pgvector](https://github.com/pgvector/pgvector), SQLite + [sqlite-vec](https://github.com/asg017/sqlite-vec)
- [Ollama](https://ollama.ai) for local inference, embeddings via `nomic-embed-text`
- [Litestream](https://litestream.io) for SQLite continuous backup

## License

MIT. See [LICENSE](LICENSE).

## Author

[AJ Crabill](https://github.com/ajcrabill). Architectural design and refactor planning by Loriah (the dCoS this project descends from).
