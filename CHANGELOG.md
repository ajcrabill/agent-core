# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Sprint 0 — repo bootstrap. Monorepo skeleton (`packages/agent-core`, `packages/dcos-agent`, `packages/ikb-agent`, `packages/hermes`). MIT license. uv workspaces. CI placeholder.

## [0.0.1] — 2026-05-02

### Added
- Repository initialized. Replaces the legacy [v1 dCoS](https://github.com/ajcrabill/dCoS/tree/legacy-v1) (preserved on `legacy-v1` branch).
- Architecture and sprint plan documented in `docs/ARCHITECTURE.md` and `docs/ROADMAP.md`.

### Changed
- Project scope expanded from single-user dCoS to a platform supporting both `dcos-agent` (personal) and `ikb-agent` (team).

### Removed
- Legacy v1 implementation moved to `legacy-v1` branch. New architecture starts fresh.
