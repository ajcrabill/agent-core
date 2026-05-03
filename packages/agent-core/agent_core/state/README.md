# `agent_core.state`

Canonical operational state. Database is the source of truth; the vault is a
generated projection.

## What's here so far (Sprint 1, in progress)

- `models.py` — SQLModel definitions for the foundation tables
- `__init__.py` — public API

## What's coming in this sprint

- More tables (intercom, audits, action_log, openbrain, content-creation)
- `db.py` — dual-backend `Database` class (sqlite default for dcos-agent;
  postgres default for ikb-agent)
- `migrations/` — Alembic config + initial migration
- `renderer.py` — db → markdown projection (vault is generated)
- `watcher.py` — markdown → db (for the few human-edited files: kanban,
  conversation journal)

## Schema overview (current commit)

```
identity            ← the agent's own identity (one row, id='self')
peer                ← discovered mesh peers
obligation          ← tasks; status: inbox|in-progress|waiting|done
obligation_event    ← append-only audit log of state transitions
plan                ← per-obligation plan (steps, current_step, status)
completion_check    ← append-only log of self-tests against criteria
learning_rule       ← supervised-learning rules; tags determine loading scope
rule_firing         ← per-firing log; powers firing visibility
correction_candidate ← auto-detected corrections awaiting promotion to rules
```

All tables compile cleanly to both SQLite and Postgres. JSON columns map to
JSONB on Postgres / JSON-as-TEXT on SQLite. Enums are str-mixin classes stored
as TEXT (no native Postgres ENUM types — keeps migrations portable).
