# dcos-agent

Personal AI chief of staff. Single-user. Built on `agent-core`.

See the [main README](../../README.md) for the full picture.

## What it does

- Triages your email to inbox-zero (the original Loriah-proven feature)
- Drafts emails and documents in your voice (supervised learning + exemplar matching)
- Tracks obligations from every channel, plans them, executes autonomously where safe
- Manages calendar with meeting prep, follow-up extraction, decision briefs
- Maintains people intelligence (last contact, context, applicable obligations)
- Coordinates with `ikb-agent` instances natively via the mesh layer

## Default storage

SQLite + Litestream for backup. Move to Postgres if you outgrow it.
