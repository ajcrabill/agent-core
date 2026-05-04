# Quickstart

**Two commands to a chatting agent.** Tested on macOS 14+ and Ubuntu/Debian Linux.

```bash
git clone https://github.com/ajcrabill/agent-core.git && cd agent-core
./bootstrap.sh
```

`bootstrap.sh` handles everything: detects/installs `uv`, runs `uv sync`, walks you through LLM provider choice (OpenAI / local Ollama / stub), writes settings, bootstraps the schema, generates an API token, runs doctor, and drops you into `dcos chat`.

Need ikb-agent instead?
```bash
./bootstrap.sh --product ikb
```

## What if you already have prereqs?

`bootstrap.sh` checks for and (where possible) installs:

- **Python 3.11+** — must be present already
- **uv** — auto-installs from astral.sh if missing
- **git** — must be present (you cloned with it)
- **submodules** — auto-fetches the OpenWebUI fork

Optional but recommended:
- `brew install ollama && ollama pull nomic-embed-text` for OpenBrain semantic search
- `brew install postgresql@16 && brew services start postgresql@16` if you'll use ikb-agent
- `brew install node` only if you want the OpenWebUI ObligationBoard plugin (separate setup)

If you're on a fresh macOS user account without Homebrew:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)" && echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zshrc
```

## Manual install (if you don't want to run a script)

```bash
git clone https://github.com/ajcrabill/agent-core.git
cd agent-core
git submodule update --init --recursive       # OpenWebUI fork (skip if you don't need the chat UI)
uv sync                                       # installs everything (dcos / ikb / agent-core / dev deps)
```

## Pick your product

**`dcos-agent`** — single-user personal AI chief of staff. SQLite by default.

**`ikb-agent`** — small-team intelligent knowledge base. PostgreSQL by default. Same install flow; replace `dcos` with `ikb` in every command below.

## Bootstrap (one command)

```bash
uv run dcos setup --tier 1
```

`setup` asks 3 questions, writes `agent.yml`, bootstraps the schema, generates an API token, and runs `doctor`. End-to-end in ~10 seconds.

The **API token** prints during setup — keep it for the OpenWebUI plugin. You can recover it later: `uv run dcos init` is idempotent and prints the existing token (use `--rotate-token` to replace it).

The token is stored in your OS keychain (macOS Keychain / Windows Credential Locker / Linux Secret Service). On headless Linux installs (VPS, Docker), it falls back to a local file at `~/.local/state/agent-core/secrets.json` (mode 0600).

Want the wizard without the chained init+doctor? `--no-init` and `--no-doctor` flags skip the tail steps.

## Wire up an LLM (one command)

```bash
# OpenAI (or any OpenAI-compatible — DeepSeek, Mistral, OpenRouter, …):
uv run dcos init --llm-provider openai_compat --llm-api-key "$OPENAI_API_KEY"

# Local Ollama (no API key needed):
uv run dcos init --llm-provider ollama --llm-model llama3.2
```

After this, the agent has a brain. The config goes into `agent.yml`, the API key into the secrets store.

## Talk to your agent

Two paths once the LLM is configured:

**Terminal REPL:**
```bash
uv run dcos chat
# you> what's on my plate today?
# agent: ...
# you> /exit
```

Auto-injects your active obligations + relevant openbrain hits into each turn. Slash commands: `/reset`, `/context` (toggle injection), `/exit`.

**Browser:**
```bash
uv run dcos serve            # http://127.0.0.1:8765
```

Then open **http://127.0.0.1:8765/chat** — vanilla HTML chat UI, no Node, no build step. Paste your bearer token once (auto-saved to localStorage), then chat. Same context-injection logic as the CLI.

Other interfaces:
- `http://127.0.0.1:8765/docs` — Swagger UI for the full REST API
- `dcos skills run email-triage --input '...'` — run individual skills
- `dcos skills list / describe <name>` — what's registered
- `dcos remember "<text>"` — quick-capture into semantic memory
- `dcos recall <query>` — semantic search across captured thoughts

Pass `--stub-llm` (CLI) or skip the LLM config entirely to run with canned-response stubs — useful for offline tests.

## Memory across chats

Every `dcos chat` turn auto-captures to OpenBrain (source_kind=`chat`). Future chats — even from different terminal sessions — surface relevant prior conversations via the same context-injection that surfaces vault notes. Turn it off with `--no-context` if you want a clean slate, or tag a session with `--system "fresh start"` etc.

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
