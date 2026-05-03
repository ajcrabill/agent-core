# Quickstart

> **Pre-release.** The install path below isn't published yet. See [ROADMAP.md](ROADMAP.md) for current sprint status. The functional install ships at the end of Sprint 10.

## When ready (target: Sprint 10)

```bash
# Personal:
pipx install dcos-agent
agent-core init                  # 3-tier interview wizard
agent-core start                 # launches launchd unit (macOS) / systemd --user (Linux)
agent-core doctor                # health check

# Team:
pipx install ikb-agent
agent-core init                  # same wizard, defaults to PG + pgvector
```

## Setup wizard

The wizard is a chat conversation, not a config file. Three tiers:

- **Just defaults** (~10 min) — answer the bare minimum, accept sensible defaults
- **Recommended** (~30 min) — also connects Gmail/Calendar, picks OpenBrain ingest sources, configures mesh peers
- **The whole thing** (~60 min) — also walks you through your first content-creation skill (e.g., "I want you to learn how to write client evaluations")

Re-run any tier later: `agent-core init --tier 2`, `agent-core init skill <name>`.

## Prerequisites

- Python 3.11+
- An OpenAI-compatible inference endpoint: local Ollama, Anthropic, OpenAI, DeepSeek, or any other compatible provider
- For ikb-agent: PostgreSQL 16+ with pgvector
- For mesh: Tailscale (recommended) or other private network for inter-agent traffic
- For Gmail / Calendar integration: a Google Cloud Console project with Gmail and Calendar APIs enabled

## Pre-release contributors

If you're working on this project before v1:

```bash
git clone https://github.com/ajcrabill/dCoS
cd dCoS
uv sync
uv run pytest
```

## Where to start reading

- [README](../README.md) — what this is and why
- [ARCHITECTURE](ARCHITECTURE.md) — how it fits together
- [ROADMAP](ROADMAP.md) — sprint plan and current status
