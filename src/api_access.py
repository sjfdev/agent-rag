"""Shared HTTP guards for the public endpoints."""

import secrets

from fastapi import HTTPException, Request

from src import auth
from src.config import settings


def require_chat_key(http_request: Request) -> None:
    """Optional API key for chat and citation endpoints (open when unset).

    In jwt mode the bearer token is the credential, so the static key is not
    required; identity and permissions are resolved per request.
    """
    if auth.auth_enabled():
        return
    expected = settings.CHAT_API_KEY
    if not expected:
        return
    provided = (
        http_request.headers.get("x-api-key")
        or http_request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid api key")
