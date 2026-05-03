"""Settings router — read-only view of the resolved settings.

The UI surfaces this so users can see what their agent thinks it is. Write
access stays on the CLI deliberately — flipping autonomy from cautious to
aggressive shouldn't be one click in a browser. (Power users can still
use ``agent settings set ...`` if they want to.)

Endpoints:

    GET /settings           — full resolved settings (with per-key sources
                              if a SettingsManager is mounted)
    GET /settings/preset    — names of available presets
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from agent_core.settings.presets import list_presets
from agent_core.web.auth import require_token

router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    dependencies=[Depends(require_token)],
)


@router.get("")
def get_settings(request: Request) -> dict[str, Any]:
    """Return the resolved AgentSettings as a dict.

    If the app was built with a SettingsManager (not a bare AgentSettings),
    each value carries its source (default / file / env) under a sibling
    ``__sources__`` key so the UI can show "this came from agent.yml" badges.
    """
    s = request.app.state.settings
    if hasattr(s, "all_with_sources"):
        # SettingsManager — include source provenance.
        snapshot: dict[str, Any] = s.settings.model_dump()
        snapshot["__sources__"] = {
            row.path: row.source.value for row in s.all_with_sources()
        }
        return snapshot
    return s.model_dump()


@router.get("/presets")
def get_presets() -> list[str]:
    return list_presets()
