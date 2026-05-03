"""HTTP Bearer auth for the agent_core.web API.

The app is configured with one or more valid bearer tokens at construction
time. ``require_token`` is a FastAPI dependency that validates the
``Authorization: Bearer <token>`` header against that set.

Constant-time comparison via ``hmac.compare_digest`` defends against the
classic length-leak timing attack — overkill for a single-user agent on
localhost, but cheap and the right reflex.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# auto_error=False so we can craft our own 401 message instead of FastAPI's
# default "Not authenticated" — gives plugin authors a clearer hint.
_bearer = HTTPBearer(auto_error=False)


class TokenStore:
    """Holds the set of valid bearer tokens. Constant-time comparisons.

    Mutable in-place — ``add()`` / ``revoke()`` cover token rotation without
    needing to rebuild the FastAPI app.
    """

    def __init__(self, tokens: set[str] | None = None) -> None:
        self._tokens = set(tokens or set())

    def is_valid(self, candidate: str) -> bool:
        return any(hmac.compare_digest(candidate, t) for t in self._tokens)

    def add(self, token: str) -> None:
        if not token:
            raise ValueError("refused to add empty token")
        self._tokens.add(token)

    def revoke(self, token: str) -> None:
        self._tokens.discard(token)

    @property
    def count(self) -> int:
        return len(self._tokens)


def install(app, store: TokenStore) -> None:
    """Stash the TokenStore on the app's state so the dependency can find it."""
    app.state.token_store = store


def require_token(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """FastAPI dependency: rejects requests without a valid bearer token.

    Returns the token string on success (handlers don't usually need it,
    but it's there for audit logging if a route wants to record who acted).
    """
    store: TokenStore | None = getattr(request.app.state, "token_store", None)
    if store is None or store.count == 0:
        # Fail closed — an unconfigured app must NOT silently accept all callers.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="agent_core.web has no auth tokens configured",
        )
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization: Bearer <token> header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not store.is_valid(creds.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return creds.credentials


__all__ = ["TokenStore", "install", "require_token"]
