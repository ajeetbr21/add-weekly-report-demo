"""API dependencies: authentication for internal endpoints."""

from __future__ import annotations

import hmac

from fastapi import Header, Request

from app.core.config import get_settings
from app.core.errors import UnauthorizedError


def _extract(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def token_valid(provided: str | None) -> bool:
    expected = get_settings().atlas_api_token.get_secret_value()
    if not expected:
        return True  # auth disabled (documented: only acceptable when bound to localhost)
    return bool(provided) and hmac.compare_digest(provided.encode(), expected.encode())


async def require_api_token(request: Request, authorization: str | None = Header(default=None),
                            x_api_key: str | None = Header(default=None)) -> str:
    """Protects /api/*. Accepts `Authorization: Bearer <ATLAS_API_TOKEN>` or `X-API-Key`."""
    provided = _extract(authorization, x_api_key)
    if not token_valid(provided):
        raise UnauthorizedError("Missing or invalid API token")
    return "api-token" if provided else "anonymous-local"
