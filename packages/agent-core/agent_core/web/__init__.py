"""agent_core.web — FastAPI HTTP API for UI plugins.

This is the surface that the OpenWebUI fork (Sprint 11b/c), and any other
front-end / external integration, talks to. Lives behind a bearer-token
auth gate (the agent generates a token at install time and the user pastes
it into their UI plugin).

Routers:

  - ``/health``       — liveness/readiness; no auth
  - ``/obligations``  — list, get, transition obligations on the board
  - ``/openbrain``    — capture + semantic search
  - ``/settings``     — read-only view of the resolved settings (so the UI
                        can show "what your agent thinks it is" without
                        granting write access by default)

The app is built via ``create_app(db, settings, *, api_token)``. Pass a
SettingsManager (not a bare AgentSettings) when you want the API to reflect
file/env changes without restart.

Auth model: HTTP Bearer. Token is opaque from the API's perspective —
generation/rotation happens via the CLI (``agent web token rotate``, lands
in Sprint 11c). For now, callers pass the token at app construction time.

Why bearer not ed25519: the OpenWebUI plugin and other browser-side
integrations can store an opaque token easily; round-tripping a signed
envelope per request is more friction than this surface needs. Mesh
signing (Sprint 6a) is still the auth model for *agent-to-agent* traffic.
"""

from agent_core.web.app import create_app

__all__ = ["create_app"]
