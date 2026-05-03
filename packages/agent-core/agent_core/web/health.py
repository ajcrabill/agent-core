"""Liveness/readiness endpoint. No auth.

Two checks:
  - liveness: process is up and serving (always 200)
  - readiness: db reachable + settings loaded (200 ok / 503 if degraded)

The OpenWebUI plugin polls /health to know whether agent-core is up; load
balancers / process supervisors can use it for restart decisions.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Simple liveness — the process responds, that's it."""
    return {"status": "ok"}


@router.get("/health/ready")
def readiness(request: Request) -> dict[str, str | bool]:
    """Verify dependencies: db query succeeds + settings present.

    Returns 200 with details so the caller sees *what* is ready (or not),
    rather than just a binary up/down."""
    db = getattr(request.app.state, "db", None)
    settings = getattr(request.app.state, "settings", None)

    db_ok = False
    if db is not None:
        try:
            from sqlmodel import select

            from agent_core.state.models import Obligation

            with db.session() as s:
                s.exec(select(Obligation).limit(1)).first()
            db_ok = True
        except Exception:
            db_ok = False

    return {
        "status": "ok" if (db_ok and settings is not None) else "degraded",
        "db": db_ok,
        "settings": settings is not None,
    }
