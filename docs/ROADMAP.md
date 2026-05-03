# Roadmap

Pre-release, ~10 weeks focused effort.

## Sprint status

| # | Sprint | Days | Status |
|---|---|---|---|
| 0 | Pre-work — repo init, key rotation, doc updates, OB consolidation, deepseek-chat workaround | 1 | 🟡 In progress |
| 1 | `agent_core.state` — schema, dual backend (sqlite/postgres), Alembic migrations, vault renderer + watcher | 5 | ⏳ |
| 2 | `agent_core.agent` — context-loader hooks (rules + obligations + intercom + incidents); replaces "remember to read" with code enforcement | 4 | ⏳ |
| 2.5 | `agent_core.agent.loop` — goal-directed agent loop (plan or execute on every active obligation) | 2 | ⏳ |
| 3 | `agent_core.work` — cron watchdog, pipeline monitor, incidents, inbound capture pipeline, plan developer, completion-criteria verifier | 5 | ⏳ |
| 4 | `agent_core.quality` — two-tier auditor, score store, undelegation, weekly report | 3 | ⏳ |
| 4.5 | `agent_core.actions` — class taxonomy, action log + rationale, daily digest synthesis | 2 | ⏳ |
| 5a | `agent_core.learning` — store (jsonl + db, dual backend), tag resolver, maintenance | 3 | ⏳ |
| 5b | Supervised-learning UX — capture detector, firing visibility, weekly review surface, pre-seed packs | 4 | ⏳ |
| 5c | `agent_core.content_creation` — exemplars, iterations, diff-extractor, calibration | 5 | ⏳ |
| 6 | `agent_core.mesh` — relay reimplemented dual-backend, ed25519 auth, MCP tool API preserved, peer discovery | 5 | ⏳ |
| 7 | `agent_core.identity` + `agent_core.secrets` + `agent_core.openbrain` (semantic search + ingest framework) | 5 | ⏳ |
| 8 | `agent_core.ops` — `agent-core doctor`, backup, restore, **3-tier interview wizard** | 4 | ⏳ |
| 9 | Test harness — fixtures, E2E, CI on macOS + Linux | 3 | ⏳ |
| 10 | Package & ship — `dcos-agent` and `ikb-agent` packages, default skills, docs, GitHub Actions release | 5 | ⏳ |
| 11 | OpenWebUI integration — skin + brand, OB plugin, shared landing page, MkDocs publish for iKB | 4 | ⏳ |
| 11.5 | Three default skill templates (email-triage, document-creator, email-composer) + 3 new ingest pipelines | 4 | ⏳ |
| 12 | Hard-cut migration: dry-run shadow, Sunday cutover, rollback procedure | 3 | ⏳ |

**Total**: ~67 working days = ~10 weeks focused, 12-14 calendar weeks realistic.

## Big rocks

The four highest-leverage outcomes:

1. **Reliability fix** (Sprints 1-2) — replace "remember to read state files" with code-enforced context injection. This alone fixes the two reliability bugs (forgotten obligations, ignored learning rules) that exist on both sides today.
2. **Goal-directed operation** (Sprint 2.5 + 3) — every action traces to an obligation with testable completion criteria. Bias for action with audit trail.
3. **Content-creation supervised learning** (Sprint 5c + 11.5) — the killer feature. Point at exemplars, iterate from raw input, agent learns to deliver reliably.
4. **Native agent collaboration** (Sprint 6) — dCoS and iKB find each other and coordinate by default.

## After v1

Capabilities planned but not in MVP:

- Voice memo ingest (Whisper-based)
- Slack / Linear ingest
- Multi-tenant ikb-agent (true RBAC, per-user identity)
- Hosted ikb-agent option (one-click deploy)
- VS Code extension (chat + tasks in editor)
- iOS / Android push notifications native (currently just email + ntfy.sh)
