# Security Policy

## Supported versions

This project is in pre-release. Security fixes will be applied to the latest `main` branch only until a v1.0.0 release.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities. Email AJ Crabill directly (contact via GitHub profile) with:

- A description of the issue
- Steps to reproduce
- Affected versions
- Any potential impact

You can expect an acknowledgment within 72 hours and a resolution timeline within one week.

## Threat model

This project handles:

- Personal email (Gmail OAuth tokens)
- Calendar (Google Calendar OAuth tokens)
- Personal vault content (markdown files in user-controlled location)
- Inter-agent messages over Tailscale or other private networks
- Knowledge ingested from a user's connected sources (Drive, GitHub, Notion, etc.)

Sensitive data is **never** committed to the repository. Secrets live in OS keychain (default) or age-encrypted files. The `.gitignore` aggressively excludes `*.env`, `*token*`, `*secret*`, `*credentials*`, `*.db`, `state/`, `vault/`, etc. Pre-commit hooks (added in Sprint 8) will scan for accidental secret commits.

## Scope of action policy enforcement

Per the action policy taxonomy (see `docs/ARCHITECTURE.md`), agents will:

- **Never** access secrets directly or perform financial actions
- **Always** require explicit confirmation for: external email send, content publishing, external calendar invites, modifying People notes, installing new skills
- **Default to autonomous** for: read operations, internal vault writes, ObligationBoard updates, mesh messages between known peers

Users can customize this policy via `agent-core init --tier 3` or `dcos action-policy set ...`.

## Reporting on the agent's autonomous actions

Per the bias-for-action design, agents act autonomously within their policy boundaries and report after the fact. Every autonomous action is logged in `action_log` with a decision rationale. The daily digest synthesizes these for human review. Users can disable autonomy at any time with `dcos pause` (Sprint 8).
