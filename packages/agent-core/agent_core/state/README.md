# `agent_core.state`

Canonical operational state. Database is the source of truth; the vault is a
generated projection.

## What's here so far (Sprint 1, in progress)

- `models.py` — SQLModel definitions for **all 26 operational tables**
- `__init__.py` — public API
- 20 schema-smoke tests in `tests/test_state_models.py`

## What's here in this sprint (Sprint 1, complete)

- `models.py` — 26 SQLModel tables
- `db.py` — dual-backend `Database` (SQLite default for dcos-agent; Postgres
  default for ikb-agent); engine factory, sessions, health check, `upgrade()`
- `migrations/` — Alembic config + initial migration; ships with the package
- `renderer.py` — db → markdown projection (idempotent, stale-sweep)
- `watcher.py` — markdown → db (debounced, fixed-point stable with renderer)

## Schema overview (current commit — 26 tables)

### Identity (2)
| table | purpose |
|---|---|
| `identity` | The agent's own identity (one row, `id='self'`) |
| `peer` | Discovered mesh peers (Sprint 6 populates) |

### Work (4) — the goal-directed core (L20)
| table | purpose |
|---|---|
| `obligation` | Tasks; status: inbox/in-progress/waiting/done; **has structured `completion_criteria`** |
| `obligation_event` | Append-only audit log of state transitions |
| `plan` | Per-obligation plan (steps array, current_step, status) |
| `completion_check` | Append-only log of self-tests against criteria |

### Learning (3)
| table | purpose |
|---|---|
| `learning_rule` | Supervised-learning rules; tags determine loading scope (`general` or skill name) |
| `rule_firing` | Per-firing log; powers firing visibility + "rules that haven't fired in 90d" |
| `correction_candidate` | Auto-detected corrections from chat awaiting promotion |

### Delegations (1)
| table | purpose |
|---|---|
| `delegation` | Work the agent (or principal) handed off; follow-up tracking |

### Run / Incidents / Actions (3)
| table | purpose |
|---|---|
| `run_log` | Per-skill-execution audit (cron, on-demand, plan-step) |
| `incident` | Failures the agent must consult before claiming completion |
| `action_log` | **Every autonomous action** — `obligation_id` REQUIRED per L20 |

### Quality (2)
| table | purpose |
|---|---|
| `quality_audit` | Per-audit results (score, primary_notes, sampling_reason) |
| `quality_score` | Running per-(model, task_type); auto-undelegation reads here |

### Mesh (2)
| table | purpose |
|---|---|
| `intercom_message` | Inter-agent message store (Sprint 6 implements wire protocol) |
| `intercom_ack` | Per-message ack log (detects silent drops) |

### Sessions / Metrics (2)
| table | purpose |
|---|---|
| `session` | Lightweight session summaries (full transcripts in Hermes' state.db) |
| `metric` | Generic time-series metric |

### Content creation (4) — Sprint 5c populates
| table | purpose |
|---|---|
| `exemplar` | Canonical "good output"; `is_synthetic` flag for L21 battery items |
| `iteration` | One (raw → attempts → corrections → final) cycle; `is_synthetic` flag |
| `template` | Starting skeleton with placeholders |
| `calibration` | Per-skill confidence + `autonomous_mode` gate |

### OpenBrain (3) — Sprint 7 adds vector column
| table | purpose |
|---|---|
| `thought` | Unit of semantic memory (Sprint 7 adds backend-conditional `embedding`) |
| `thought_source` | Provenance + freshness + authority + visibility ACL hint |
| `ingestion_run` | Per-pipeline-run audit |

## Design choices (encoded in tests as regression guards)

- Enums use `StrEnum` (Python 3.11+); columns mapped to `VARCHAR(32)` on both
  backends — **no native PG ENUM types**, keeps migrations portable.
- JSON columns use SQLAlchemy's `JSON` type → `JSONB` on Postgres, `JSON-as-TEXT`
  on SQLite.
- Foreign keys use string IDs (UUID-as-text) for cross-table portability.
- Surrogate integer PKs only for high-frequency append-only tables
  (events, rule firings, completion checks, run log, etc.).
- `obligation.plan_id` deliberately NOT defined — derives the active plan via
  `SELECT * FROM plan WHERE obligation_id=? AND status != 'verified' ORDER BY
  created_at DESC LIMIT 1`. Avoids FK cycle.
- `action_log.obligation_id` is **NOT NULL** — every autonomous action traces to
  an obligation per L20. Test guards against regression.
- `exemplar.is_synthetic` and `iteration.is_synthetic` distinguish natural
  training data from L21 synthetic-battery items (so calibration can detect
  overfit-to-synthetic).
