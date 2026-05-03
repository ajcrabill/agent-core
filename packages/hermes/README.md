# Hermes (forked)

This directory will hold a fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent), vendored as a git subtree.

## Plan

- Sprint 0 (now): placeholder — fork strategy locked, vendoring deferred to Sprint 1
- Sprint 1: vendor upstream `v0.12.0` as git subtree at this path
- Ongoing: integrate upstream changes via `git subtree pull`; document local patches in `PATCHES.md`

## Why we fork

`agent-core` modifies Hermes runtime behavior (custom context-loader hooks, action-policy enforcement, the goal-directed agent loop). These changes are too invasive to maintain as monkey-patches; a fork is cleaner.

The fork is intended to **stay close to upstream** — we accept all upstream changes that don't conflict, and contribute improvements back where they make sense.

## What's modified vs. upstream

To be documented in `PATCHES.md` once vendoring is complete.

Initial known patches (likely):

- Context-loader hook injection points (Sprint 2)
- Action-policy enforcement at tool-call boundary (Sprint 4.5)
- Goal-directed agent-loop orchestration (Sprint 2.5)
- DeepSeek auxiliary auto-detect fallback bug fix (currently breaks 4 cron jobs on Esby; see `docs/ARCHITECTURE.md`)
