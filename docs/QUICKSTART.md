# Quickstart

Five commands to a working agent install. Tested on macOS 14+ with Python 3.11.

## Install

```bash
# Prereqs (one-time)
brew install uv git
# Optional: brew install node                                     # for OpenWebUI ObligationBoard plugin
# Optional: brew install ollama && ollama pull nomic-embed-text   # for OpenBrain semantic search
# Optional (ikb-agent only): brew install postgresql@16 && brew services start postgresql@16

# Clone + install
git clone https://github.com/ajcrabill/agent-core.git
cd agent-core
git submodule update --init --recursive       # OpenWebUI fork (skip if you don't need the chat UI)
uv sync --all-extras                          # installs dcos-agent, ikb-agent, agent-core, dev deps
```

## Pick your product

**`dcos-agent`** — single-user personal AI chief of staff. SQLite by default.

**`ikb-agent`** — small-team intelligent knowledge base. PostgreSQL by default. Same install flow; replace `dcos` with `ikb` in every command below.

## Bootstrap (run once)

```bash
uv run dcos setup --tier 1     # 3 questions: preset / display name / sqlite|postgres
uv run dcos init               # creates schema + generates an API token (printed once)
uv run dcos doctor             # health check; should report 5+ ok, 0 fail
```

After `init`, the **API token** prints once. Keep it — the OpenWebUI plugin needs it. You can recover it any time by re-running `dcos init` (idempotent; prints the existing token unless `--rotate-token` is passed).

The token is stored in your OS keychain (macOS Keychain / GNOME Keyring / KWallet) by default. On headless systems it falls back to env vars; `dcos init` will tell you what to set.

## Day-to-day

```bash
# Start the HTTP API (the OpenWebUI plugin's backend)
uv run dcos serve                # http://127.0.0.1:8765 — see /docs for OpenAPI

# Inspect installed skills
uv run dcos skills list
uv run dcos skills describe email-triage

# Run a skill (uses StubLanguageModel for now; real LLM lands with Hermes)
uv run dcos skills run email-triage --input '{"sender":"x@y","subject":"hi","body":"test"}'

# Settings
uv run dcos settings show
uv run dcos settings set notifications.enabled=true
uv run dcos settings preset apply cautious

# Backup / restore
uv run dcos backup ~/snapshots/$(date +%F).json --db-url "$(uv run dcos settings show storage.url --json | jq -r '.[0].value')"
uv run dcos restore ~/snapshots/2026-05-03.json --yes
```

## Migrate from an existing setup

If you're migrating from the old Loriah / Esby installs, see:
- [MIGRATION.md](../MIGRATION.md) — Loriah's Obsidian vault → dcos-agent
- [ESBY_MIGRATION.md](../ESBY_MIGRATION.md) — Esby's installed-chief-of-staff → ikb-agent

## Add the OpenWebUI ObligationBoard

Once `dcos serve` is up, start the OpenWebUI fork in another terminal:

```bash
cd packages/open-webui-fork
npm install                    # one-time, ~1 minute
npm run dev                    # http://localhost:5173
```

Open http://localhost:5173/obligations — the Settings panel will prompt for your agent-core URL (`http://127.0.0.1:8765`) + the API token from `dcos init`.

## Where things live

| Path | What |
|---|---|
| `~/.config/dcos-agent/agent.yml` | Settings overlay (the wizard writes this) |
| `~/.local/state/dcos-agent/agent.db` | SQLite database (dcos default) |
| OS keychain | API token, identity keys |

`uv run dcos info` prints all resolved paths. Useful in bug reports.

## Health checks

```bash
uv run dcos doctor             # 7 checks; exits non-zero on any fail
uv run dcos doctor --json      # structured output for monitoring
```

Each check is skippable — Ollama only checked when `embedding_provider=ollama`, vault only when configured, etc.

## Prerequisites in detail

- **Python 3.11+** (3.12 also works)
- **uv** (`brew install uv` or https://docs.astral.sh/uv/)
- **git** (any recent version)
- **Optional — Node.js + npm** for the OpenWebUI ObligationBoard plugin. Skip entirely if you only need the CLI + the auto-generated Swagger UI at `http://127.0.0.1:8765/docs`.
- **Optional — Ollama** for OpenBrain semantic search. Without it, OpenBrain falls back to deterministic stub embeddings (works but no real similarity).
- **Optional — PostgreSQL 16+** for ikb-agent. SQLite works for dcos-agent out of the box.
- **Optional — Tailscale** for cross-machine mesh between dcos and ikb instances.

## Pre-release contributors

```bash
git clone https://github.com/ajcrabill/agent-core.git
cd agent-core
git submodule update --init --recursive
uv sync --all-extras           # critical: --all-extras pulls dev deps (pytest, ruff, mypy)
uv run pytest                  # should be all green
```

## Where to read next

- [README](../README.md) — what this is and why
- [ARCHITECTURE](ARCHITECTURE.md) — how it fits together
- [ROADMAP](ROADMAP.md) — sprint plan and current status
