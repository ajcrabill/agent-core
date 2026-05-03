# Architecture

> **This document is a public summary.** The full design — including all locked decisions, sprint-by-sprint plan, schema, mesh protocol, action-policy taxonomy, content-creation pipeline, setup wizard tiers, and verification of pre-existing reliability bugs — lives in the maintainer's design notes (not in this repo).

## Core principle

**Goal-directed operation.** Every inbound (email, chat, peer message) spawns an obligation. Every obligation has testable completion criteria. Every autonomous action traces back to an obligation. The agent loops: while there are active agent-owned obligations, develop a plan or execute the next plan step. Sleep only when nothing is actionable.

This is the principle that makes "bias for action" safe and auditable.

## Three packages

```
agent-core              the platform — state, learning, work, agent, quality,
                        mesh, openbrain (semantic search + ingest), content-creation,
                        actions (policy + log), ops (doctor, backup, init wizard)

dcos-agent              personal chief-of-staff product
                        backend: SQLite (default)
                        surfaces: OpenWebUI chat, Obsidian vault, ObligationBoard
                        default skills: email-triage, document-creator, email-composer,
                          meeting-prep, followup-extract, decision-brief, people-dossier,
                          daily-briefing

ikb-agent               team intelligent-knowledge-base product
                        backend: PostgreSQL + pgvector (default)
                        surfaces: OpenWebUI chat, MkDocs vault view, ObligationBoard
                        default skills: librarian, knowledge-harvester, document-creator,
                          email-composer, newsletter-creator, meeting-evaluator,
                          decision-matrix, confidence-scoring
```

Both packages depend on `agent-core`. Mesh layer is enabled by default in both, so dCoS↔iKB collaboration is native.

## Storage

Single SQLModel schema, two backends:

- `sqlite` (default for dcos-agent) — Litestream for backup
- `postgres` (default for ikb-agent) — pgvector for semantic memory; pg_dump for backup

Markdown projection: any write to operational tables regenerates the corresponding `.md` files in the vault. The vault is the human projection; the database is the source of truth.

## Hermes runtime

This project carries a fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) at `packages/hermes/`. The fork starts clean from upstream v0.12.0; future upstream changes are integrated as we receive them. Local patches are documented in `packages/hermes/PATCHES.md`.

## Action policy

Three classes:

- **Autonomous**: read, write-internal, OB updates, cross-agent messages, calendar reads, ingest pipelines, exemplar capture
- **Gated** (one-click human confirmation): send email to external party, publish content, create calendar invite for external party, modify People notes, install new skill from catalog
- **Forbidden**: secret access, financial actions, modifying safety policies

Users override per-action and per-class.

## Mesh

Postgres-backed (or SQLite for personal-scale), HTTP+JSON wire protocol, ed25519 peer authentication, at-least-once delivery, idempotent receive, explicit ack on processing. MCP tool API (`team_send_message`, etc.) preserved for backward compat with existing implementations.

## Web UI

Best-in-class tools at the edges, not reinvented:

- **OpenWebUI** for chat (skinned + branded; ObligationBoard plugin lets the agent manipulate tasks from inside chat)
- **ObligationBoard** built into `agent-core` (kanban: 4 columns — Inbox / In Progress / Waiting / Done)
- **MkDocs Material** for iKB read-only knowledge publishing
- **Obsidian** for dCoS local vault editing

A unified shell page at the install root presents the three tiles.

## Setup wizard

Three tiers via `agent-core init`:

- **Tier 1 (5-10 min)**: Required — name, mail, model provider, storage, vault path
- **Tier 2 (15-30 min)**: Recommended — Gmail/Calendar OAuth, OpenBrain ingest sources, mesh peers, daily-digest schedule
- **Tier 3 (45-60 min)**: Power user — content-creation skill definitions, bulk people import, action-policy overrides, multi-identity, skill catalog

User picks "Just defaults" (Tier 1), "Recommended" (Tier 1+2), or "The whole thing" (all three).

## Sprint plan

See [ROADMAP.md](ROADMAP.md).
