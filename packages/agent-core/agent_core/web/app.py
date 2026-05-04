"""FastAPI app factory.

``create_app(db, settings, *, api_tokens, openbrain=None)`` builds a fully-
wired FastAPI app with the four routers (health, obligations, openbrain,
settings) and a TokenStore preloaded with ``api_tokens``.

Mount this however you serve FastAPI:

    app = create_app(db, settings_manager, api_tokens={"…"})
    uvicorn.run(app, host="127.0.0.1", port=8765)

CORS is permissive on localhost-only by default — the expected deployment
is OpenWebUI on the same host. Cross-origin from arbitrary domains stays
off (configurable when you actually need it)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_core.openbrain import OpenBrainStore
from agent_core.state.db import Database
from agent_core.web import auth as auth_module
from agent_core.web.chat import router as chat_router
from agent_core.web.health import router as health_router
from agent_core.web.obligations import router as obligations_router
from agent_core.web.openbrain import router as openbrain_router
from agent_core.web.settings_router import router as settings_router


def create_app(
    db: Database,
    settings: Any,
    *,
    api_tokens: set[str],
    openbrain: OpenBrainStore | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Build the FastAPI app with all default routers.

    Args:
        db: agent-core Database.
        settings: AgentSettings or SettingsManager. The settings router
            surfaces source provenance when a manager is passed.
        api_tokens: Bearer tokens accepted by the auth middleware. Must be
            non-empty — fail closed.
        openbrain: Optional OpenBrainStore. If None, one is built from
            settings (so callers don't have to wire it themselves).
        cors_origins: Allowed origins. Defaults to localhost+127.0.0.1 on
            common dev ports — wide enough for local OpenWebUI, narrow
            enough that arbitrary websites can't probe the API.
    """
    if not api_tokens:
        raise ValueError(
            "create_app requires at least one bearer token in api_tokens "
            "(pass `agent web token rotate` output, or set programmatically)"
        )

    if openbrain is None:
        openbrain = OpenBrainStore.from_settings(
            settings.settings if hasattr(settings, "settings") else settings,
            db,
        )

    app = FastAPI(
        title="agent-core",
        version=_agent_core_version(),
        description="HTTP API for the OpenWebUI fork and other UI integrations.",
    )

    # State accessible from every route via request.app.state
    app.state.db = db
    app.state.settings = settings
    app.state.openbrain = openbrain

    # Auth
    auth_module.install(app, auth_module.TokenStore(api_tokens))

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or _default_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(health_router)
    app.include_router(obligations_router)
    app.include_router(openbrain_router)
    app.include_router(settings_router)
    app.include_router(chat_router)

    return app


def _default_cors_origins() -> list[str]:
    """Localhost-only by default. OpenWebUI typically ships on :8080 or
    similar; the user can override when they deploy elsewhere."""
    return [
        "http://localhost",
        "http://localhost:8080",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]


def _agent_core_version() -> str:
    try:
        from importlib.metadata import version

        return version("agent-core")
    except Exception:
        return "0.0.1"


__all__ = ["create_app"]
