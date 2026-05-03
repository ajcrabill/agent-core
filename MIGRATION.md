# Migration runbook — Loriah → dcos-agent

Step-by-step plan for migrating Loriah's vault state into a fresh
`dcos-agent` install on the `dCoS@esblaptop` user account. Read end-to-end
before executing any step. Every command is scoped so the **old Loriah
keeps running undisturbed** during and after the cutover — there is no
hard-cutover step in this plan.

## What this migration does

- Reads three markdown files from your Obsidian vault:
  - `Admin/Loriah Skills/context-loader/operational-state.md`
  - `Admin/Loriah Skills/context-loader/conversation-journal.md`
  - `Admin/Loriah Skills/learning-log/learning-log-data.md`
- Splits each by `##` / `###` headings into ~20 markdown sections.
- Writes them into the new install as **Thoughts** in OpenBrain (every
  section keeps its source provenance — `source_kind="vault"`,
  `source_uri=<relative path>`, `source_title=<heading>`).
- Creates **4 seed Obligations** for the active threads currently visible
  in the vault (CMS Board Meeting review, Charlotte Grinberg dinner,
  Track-Charlotte People note, Drive share approval). Each lands in the
  inbox with `principal_ratification` as its completion criterion — you
  decide what to keep / drop / promote.
- Writes `agent.yml` with the `balanced` preset and your vault path so
  the watcher works out of the box.

## What this migration does NOT do

- Does not touch the source vault (read-only).
- Does not touch the existing Loriah install or its database.
- Does not migrate Esby (separate sprint; needs ssh access to a different
  machine).
- Does not migrate API keys, identity, or secrets — those get generated
  fresh during the install wizard.
- Does not migrate the `vault.db` SQLite file — those tables are empty in
  the source (loriah_learning_rules, loriah_conversation_journal, etc. all
  have 0 rows).

## Pre-flight (do once)

Run on the source machine where the vault lives. None of these touch
state.

```bash
cd ~/dev/agent-core

# 1. Confirm the vault has the three expected files.
uv run dcos migrate from-loriah-vault \
  "$HOME/Documents/Obsidian Vault" \
  --output /tmp/loriah-migration.json \
  --dry-run

#    Expected output: a table with non-zero counts and "0 missing files".
#    If you see missing files, double-check the vault path before going on.

# 2. Produce the actual migration JSON.
uv run dcos migrate from-loriah-vault \
  "$HOME/Documents/Obsidian Vault" \
  --output /tmp/loriah-migration.json \
  --preset balanced

#    Expected: "wrote backup /tmp/loriah-migration.json (NN bytes)".

# 3. Sanity-check the JSON before restore.
jq '.manifest' /tmp/loriah-migration.json
jq '.tables.obligation | map({title, status, owner, priority})' /tmp/loriah-migration.json
jq '.tables.thought | length' /tmp/loriah-migration.json
```

## Install on `dCoS@esblaptop`

Run as the `dCoS` user on the target machine. The XDG-based defaults put
the new install at `~/.local/state/dcos-agent/agent.db` and
`~/.config/dcos-agent/agent.yml` — both fresh, no collision with the old
Loriah install.

```bash
# 1. Install the workspace into a venv on the target machine.
git clone https://github.com/ajcrabill/agent-core.git ~/dev/agent-core
cd ~/dev/agent-core
git submodule update --init --recursive
uv sync

# 2. Bootstrap a fresh install (interactive — Tier 1 = three questions).
uv run dcos setup --tier 1
#    Answers: balanced / your name / sqlite

# 3. Run the doctor to confirm the bare install is healthy.
uv run dcos doctor
#    Expected: settings ok, identity ok, ollama ok if you have it,
#    everything else skipped (no vault yet, no db yet).

# 4. Copy the migration JSON over from the source machine.
#    From source machine (run BEFORE this section):
#      scp /tmp/loriah-migration.json dCoS@esblaptop:/tmp/

# 5. Bootstrap the schema (creates tables in the fresh sqlite db).
uv run python -c "
from dcos_agent import Database, default_db_url
db = Database(default_db_url())
db.create_all()
print('schema bootstrapped at', default_db_url())
"

# 6. Restore the migration into the fresh db.
uv run dcos restore /tmp/loriah-migration.json --skip-schema-check --yes
#    Expected: "restored N rows into M tables".

# 7. Verify.
uv run dcos doctor
uv run python -c "
from dcos_agent import Database, default_db_url
from agent_core.state.models import Obligation, Thought
from sqlmodel import select
db = Database(default_db_url())
with db.session() as s:
    obs = list(s.exec(select(Obligation)).all())
    thoughts = list(s.exec(select(Thought)).all())
print(f'{len(obs)} obligations, {len(thoughts)} thoughts')
for o in obs:
    print(f'  - {o.status.value:12} {o.title}')
"
```

Expected verification output (numbers may vary as the vault evolves):

```
4 obligations, 20 thoughts
  - in_progress  Review CMS Board Meeting Evaluation final version
  - inbox        Confirm May 11 dinner plans with Charlotte Grinberg
  - inbox        Track Charlotte Grinberg in People notes
  - inbox        Approve / decline Drive share request for 'Effective Strategic Planning'
```

## Post-migration (optional polish)

```bash
# Index the imported Thoughts for semantic search. Skip if you don't
# have ollama running; OpenBrain will be queryable but with stub
# embeddings only.
uv run python -c "
from dcos_agent import Database, OpenBrainStore, AgentSettings, default_db_url
from agent_core.state.models import Thought
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

# Show the imported obligations on the ObligationBoard.
# Start the agent_core.web server, then the OpenWebUI fork — see
# packages/open-webui-fork/ for `npm run dev` instructions.
```

## Rollback

There's nothing to roll back at the source — the migration is read-only
against the vault. To roll back the *target* (the new install):

```bash
# On dCoS@esblaptop:
rm -rf ~/.local/state/dcos-agent ~/.config/dcos-agent
```

The old Loriah install is on a different user account and is unaffected
by anything in this runbook.

## Re-running

Safe to re-run the migration script as many times as you want — it's
read-only. Restoring is destructive on the target db (replaces every
table); use a different target db url to test runs without overwriting
the live install:

```bash
# Test against a sidecar db
uv run dcos restore /tmp/loriah-migration.json \
  --db-url sqlite:///tmp/dcos-test.db \
  --skip-schema-check --yes
```

## When this runbook gets stale

The vault evolves. The seed-obligation list in
`packages/agent-core/agent_core/migrations/from_loriah_vault.py` is
explicitly hardcoded so you always know what gets created. If your active
threads change before you migrate, edit `SEED_OBLIGATIONS` to match what's
true today — or use `--no-seed-obligations` and create them by hand from
the imported Thoughts.
