"""Identity and authentication.

Two modes, selected by `AUTH_MODE`:

- ``none`` (default): backwards compatible. `user_id`/`user_dept` come from the
  request body, optionally gated by the static `CHAT_API_KEY`.
- ``jwt``: a signed bearer token supplies the identity. The request body may
  still contain user fields for older clients, but they are **ignored** — the
  token is the only source of `user_id`, departments, groups and admin rights.

Tokens are verified locally with a shared secret (HS256), so any gateway or
identity provider that can sign a JWT can be used without extra dependencies.
`scripts/mint_token.py` issues tokens for operations and local testing.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

import jwt

from src.config import settings

logger = logging.getLogger("agent-rag.auth")


class AuthError(Exception):
    """Raised when a token is missing, malformed, expired or wrongly signed."""


@dataclass(frozen=True)
class Identity:
    user_id: str
    departments: Tuple[str, ...] = ()
    groups: Tuple[str, ...] = ()
    is_admin: bool = False


def auth_enabled() -> bool:
    return settings.AUTH_MODE == "jwt"


def _secret() -> str:
    secret = (settings.AUTH_JWT_SECRET or "").strip()
    if not secret:
        raise AuthError("AUTH_JWT_SECRET is not configured")
    return secret


def validate_config() -> None:
    """Fail fast on an insecure configuration (called at startup)."""
    if not auth_enabled():
        return
    secret = (settings.AUTH_JWT_SECRET or "").strip()
    if len(secret) < 32:
        raise RuntimeError(
            "AUTH_MODE=jwt requires AUTH_JWT_SECRET with at least 32 characters"
        )


def encode_token(
    identity: Identity,
    ttl_seconds: Optional[int] = None,
    *,
    now: Optional[float] = None,
) -> str:
    """Mint a bearer token for an identity."""
    issued = int(now if now is not None else time.time())
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.AUTH_TOKEN_TTL)
    claims: dict = {
        "sub": identity.user_id,
        "depts": list(identity.departments),
        "groups": list(identity.groups),
        "admin": bool(identity.is_admin),
        "iat": issued,
        "exp": issued + ttl,
    }
    if settings.AUTH_JWT_ISSUER:
        claims["iss"] = settings.AUTH_JWT_ISSUER
    if settings.AUTH_JWT_AUDIENCE:
        claims["aud"] = settings.AUTH_JWT_AUDIENCE
    return jwt.encode(claims, _secret(), algorithm=settings.AUTH_JWT_ALGORITHM)


def decode_token(token: str, *, now: Optional[float] = None) -> Identity:
    """Verify a token and return its identity. Raises AuthError on any problem."""
    if not token:
        raise AuthError("missing token")
    options = {"require": ["exp", "sub"]}
    kwargs: dict = {"algorithms": [settings.AUTH_JWT_ALGORITHM], "options": options}
    if settings.AUTH_JWT_ISSUER:
        kwargs["issuer"] = settings.AUTH_JWT_ISSUER
    if settings.AUTH_JWT_AUDIENCE:
        kwargs["audience"] = settings.AUTH_JWT_AUDIENCE
    try:
        claims = jwt.decode(token, _secret(), **kwargs)
    except Exception as exc:
        raise AuthError(str(exc)) from exc

    if now is not None and int(claims.get("exp", 0)) < int(now):
        raise AuthError("token expired")

    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        raise AuthError("token has no subject")

    return Identity(
        user_id=user_id,
        departments=_clean_list(claims.get("depts")),
        groups=_clean_list(claims.get("groups")),
        is_admin=bool(claims.get("admin")),
    )


def _clean_list(value: Any) -> Tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable):
        return ()
    cleaned = (str(item).strip() for item in value)
    return tuple(dict.fromkeys(item for item in cleaned if item))


def bearer_token(headers: Any) -> str:
    """Extract a bearer token from headers (case-insensitive)."""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    auth = lowered.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Some gateways forward the raw token in X-Auth-Token.
    return lowered.get("x-auth-token", "").strip()


def identity_from_headers(headers: Any) -> Optional[Identity]:
    """Decode the caller identity, or None when auth is disabled.

    In jwt mode a missing/invalid token raises AuthError; callers map that to
    HTTP 401.
    """
    if not auth_enabled():
        return None
    token = bearer_token(headers)
    if not token:
        raise AuthError("missing bearer token")
    return decode_token(token)


def apply_identity(identity: Optional[Identity], departments: List[str],
                   groups: List[str]) -> Tuple[List[str], List[str], str]:
    """Return the effective departments/groups/user for a request.

    With an identity, everything comes from the token and the request body is
    ignored: a caller cannot widen its own permissions by editing the payload.
    """
    if identity is None:
        return departments, groups, ""
    return list(identity.departments), list(identity.groups), identity.user_id
