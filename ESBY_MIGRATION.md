# Migration runbook — Esby → ikb-agent

Step-by-step plan for migrating Esby's `installed-chief-of-staff/` state
into a fresh `ikb-agent` install on the `iKB@esbserver` user account
(esbserver in your Tailscale network). Read end-to-end before executing.

The cutover model is **gradual**: old Esby keeps running undisturbed; new
ikb-agent installs alongside, gets the migrated seed data, and absorbs
workload as you build trust in it.

## What this migration does

Reads from `~/Documents/installed-chief-of-staff/` on `esby@esbserver`:

- **`state/chief_of_staff.sqlite`**:
  - `people` (15 rows in the live source) → 15 **Person** rows
  - `policy_rules` (7 enabled rows) → 7 **LearningRule** rows tagged
    against the relevant skills (email-composer for send_email rules,
    "general" for system-level rules)
- **`config/*.yaml`** (6 files):
  - `preferences.yaml`'s `autonomy_bias` → `agent.yml`'s
    `autonomy.default_policy` (option_a→cautious, option_b→balanced,
    option_c→aggressive)
  - Every config also stored as a **Thought** with `source_kind="esby_config"`
    so design context stays searchable
- **`setup-report.md`** → 1+ **Thought** for the install record

The output is the same JSON shape `agent_core.ops.restore_backup` accepts,
so the install workflow is just: install → run migration → restore.

## What this migration does NOT do

- Read-only against the source. Never writes.
- Does not touch the existing Esby install or its data.
- Does not include `~/.old EsbyVault/Esby/` markdown by default
  (use `--include-old-vault` to opt in — adds searchable historical
  context but bloats the imported Thoughts).
- Does not migrate API keys, identity, or secrets — generated fresh
  during the install wizard.
- Does not migrate empty tables (workflow_runs has 6 historical rows
  but no agent-core equivalent; obligations/threads/events/etc. are
  empty in the live source as of Sprint 13 discovery).

## Pre-flight (do once on your Mac)

```bash
cd ~/dev/agent-core

# 1. Pull a fresh copy of Esby's source data (read-only on his side).
mkdir -p /tmp/esby-source/state /tmp/esby-source/config
scp esby@esbserver:'~/Documents/installed-chief-of-staff/state/chief_of_staff.sqlite' \
    /tmp/esby-source/state/
scp esby@esbserver:'~/Documents/installed-chief-of-staff/config/*.yaml' \
    /tmp/esby-source/config/
scp esby@esbserver:'~/Documents/installed-chief-of-staff/setup-report.md' \
    /tmp/esby-source/

# 2. Sanity-check what would migrate.
uv run ikb migrate from-esby-install /tmp/esby-source --output /tmp/esby.json --dry-run
#    Expected: 15 people, 7 learning rules, ~10 thoughts, 0 skipped.
#    If counts differ from your expectations, inspect /tmp/esby-source/ before
#    going further.

# 3. Produce the actual migration JSON.
uv run ikb migrate from-esby-install /tmp/esby-source --output /tmp/esby.json

# 4. Sanity-check the JSON.
jq '.manifest' /tmp/esby.json
jq '.tables.person | map({name, stakeholder_class, never_autonomous_send})' /tmp/esby.json
jq '.tables.learning_rule | map({source, skill_tags, correction})' /tmp/esby.json
```

## Install on `iKB@esbserver`

ikb-agent uses Postgres by default. If `iKB@esbserver` doesn't have the
target Postgres database created yet, do that first.

```bash
# All commands from here run as the iKB user on esbserver.
ssh iKB@esbserver

# 1. Install the workspace.
git clone https://github.com/ajcrabill/agent-core.git ~/dev/agent-core
cd ~/dev/agent-core
git submodule update --init --recursive
uv sync

# 2. Install postgres locally if not already present, then create the db.
#    (Skip if you already have postgres + ikb_agent db.)
brew install postgresql@16
brew services start postgresql@16
createdb ikb_agent

# 3. Bootstrap the install (Tier 1 = three questions).
uv run ikb setup --tier 1
#    Answers: balanced / iKB / postgres
#    OR override the DB url:
#    IKB_DB_URL=postgresql://localhost/ikb_agent uv run ikb setup --tier 1

# 4. Doctor.
uv run ikb doctor
#    Expected: settings ok, identity ok, db reachable.

# 5. Copy the migration JSON over from your Mac.
#    From your Mac (BEFORE this section):
#      scp /tmp/esby.json iKB@esbserver:/tmp/

# 6. Bootstrap the schema (alembic migrations including the new person table).
uv run python -c "
from agent_core.state import Database
from ikb_agent import default_db_url
db = Database(default_db_url())
db.create_all()
print('schema bootstrapped at', default_db_url())
"

# 7. Restore the migration into the fresh db.
uv run ikb restore /tmp/esby.json --skip-schema-check --yes

# 8. Verify.
uv run ikb doctor
uv run python -c "
from agent_core.state import Database, Person, LearningRule, Thought
from agent_core.people import PeopleStore
from ikb_agent import default_db_url
from sqlmodel import select
db = Database(default_db_url())
people = PeopleStore(db)
print(f'People imported: {people.count()}')
with db.session() as s:
    rules = list(s.exec(select(LearningRule)).all())
    thoughts = list(s.exec(select(Thought)).all())
print(f'LearningRules: {len(rules)}, Thoughts: {len(thoughts)}')
print('— never_autonomous_send people —')
for p in people.list(never_autonomous_send=True):
    print(f'  • {p.name:20s}  {p.stakeholder_class}')
print('— rules by skill_tag —')
for r in rules:
    print(f'  • {r.source}: tags={r.skill_tags}')
"
```

Expected verification output (numbers may shift as Esby's source data evolves):

```
People imported: 15
LearningRules: 7, Thoughts: 10
— never_autonomous_send people —
  • Charlotte             family_member
  • Jessica               principal_client
  • Monica                principal_client
  ...
— rules by skill_tag —
  • esby-policy:global_no_send_principal_client: tags=['email-composer', 'general']
  • esby-policy:internal_low_priority_reply: tags=['email-composer', 'general']
  ...
```

## Post-migration (optional)

```bash
# Index imported config Thoughts for semantic search.
uv run python -c "
from agent_core.state import Database, Thought
from agent_core.openbrain import OpenBrainStore
from agent_core.settings import AgentSettings
from ikb_agent import default_db_url
from sqlmodel import select
db = Database(default_db_url())
store = OpenBrainStore.from_settings(AgentSettings(), db)
with db.session() as s:
    ids = [t.id for t in s.exec(select(Thought)).all()]
print(f'reindexing {len(ids)} thoughts...')
for tid in ids:
    store.reindex(tid)
print('done')
"

# Start agent_core.web for the OpenWebUI ObligationBoard plugin.
# (See packages/open-webui-fork/ for `npm run dev` instructions.)
```

## Rollback

Migration is read-only on the source. To roll back the *target*
(the new ikb-agent install):

```bash
# On iKB@esbserver:
dropdb ikb_agent
rm -rf ~/.local/state/ikb-agent ~/.config/ikb-agent
```

Old Esby is on a different user account and is unaffected.

## Re-running

Migration is safe to re-run as many times as you like. Restoring is
destructive on the target db (replaces every table); use a different db
URL to test runs without overwriting the live install:

```bash
IKB_DB_URL=postgresql://localhost/ikb_agent_test uv run ikb restore /tmp/esby.json \
  --skip-schema-check --yes
```

## Known scope choices (lossy mappings to flag)

These were called out at planning time and are documented here so future-you
remembers what was dropped:

- **Esby's policy_rules → LearningRules is lossy.** Esby's `approval_required
  at confidence_threshold=0.9` doesn't have a 1:1 agent-core equivalent
  (the closest is the per-action ActionPolicy, which is per-action-class
  not per-stakeholder-class). The migration encodes the rule as natural-
  language guidance the LLM follows; properly modeling per-person /
  per-stakeholder autonomy overlays is on the deferred list (see "Future
  work" below).
- **`never_autonomous_send_default` from autonomy-matrix.yaml** lists
  classes that should default to no-autonomous-send. Per-person rows in
  these classes get `metadata_json.implicit_no_autonomous_send_class=true`
  so the value is preserved without inferring `never_autonomous_send=true`
  on rows where Esby explicitly set it False.
- **`person_emails` table is empty** in the source. If you populate it
  later and want emails imported, extend `_person_from_esby_row` in
  `agent_core/migrations/from_esby_install.py`.
- **`workflow_runs`** (6 rows) skipped — historical execution data
  without an agent-core target.

## Future work

- **Per-person + per-stakeholder-class autonomy overlay** as native
  agent-core schema (rather than the LearningRule workaround the migration
  uses today). This would let `effective_autonomy(person, settings)`
  consult both the install-wide preset and per-stakeholder-class policy.
  Sprint 14 candidate.
- **ContactMethod table** (rather than the JSON column) when the access
  patterns warrant it.
