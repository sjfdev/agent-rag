"""Identity and authentication.

Two modes, selected by `AUTH_MODE`:

- ``none`` (default): backwards compatible. `user_id`/`user_dept` come from the
  request body, optionally gated by the static `CHAT_API_KEY`.
- ``jwt``: a signed bearer token supplies the identity. The request body may
  still contain user fields for older clients, but they are **ignored** — the
  token is the only source of `user_id`, departments, groups and admin rights.

Two verification backends:

- shared secret (HS256) — the default, used by `scripts/mint_token.py`, a
  gateway, or any service that can sign a token;
- a JWKS (Keycloak, Auth0, Entra ID, Feishu/OIDC, ...) — set
  ``AUTH_JWKS_URL`` or ``AUTH_JWKS_FILE`` and tokens are verified with the
  provider's public keys. Keys are cached and refreshed on unknown `kid`.

Which claims carry the user, departments, groups and the admin flag is
configurable (`AUTH_CLAIM_*`, `AUTH_ADMIN_ROLE`, `AUTH_ROLE_DEPT_PREFIX`), so a
provider's native token can often be used without rewriting it.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import jwt

from src.config import settings

logger = logging.getLogger("agent-rag.auth")


class AuthError(Exception):
    """Raised when a token is missing, malformed, expired or wrongly signed."""


# --- JWKS handling ---------------------------------------------------------
_jwks_lock = threading.Lock()
_jwks_cache: Dict[str, Any] = {"keys": None, "fetched": 0.0, "url": ""}


def _jwks_configured() -> bool:
    return bool((settings.AUTH_JWKS_URL or "").strip() or (settings.AUTH_JWKS_FILE or "").strip())


def _fetch_jwks() -> Dict[str, Any]:
    """Fetch the key set from the URL or file, with a short cache."""
    url = (settings.AUTH_JWKS_URL or "").strip()
    path = (settings.AUTH_JWKS_FILE or "").strip()
    now = time.monotonic()
    with _jwks_lock:
        fresh = (
            _jwks_cache["keys"] is not None
            and now - _jwks_cache["fetched"] < settings.AUTH_JWKS_CACHE_SECONDS
            and _jwks_cache["url"] == (url or path)
        )
        if fresh:
            return _jwks_cache["keys"]

    if path:
        with open(path, "r", encoding="utf-8") as handle:
            keys = json.load(handle)
    else:
        import requests

        response = requests.get(url, timeout=5)
        response.raise_for_status()
        keys = response.json()

    with _jwks_lock:
        _jwks_cache["keys"] = keys
        _jwks_cache["fetched"] = time.monotonic()
        _jwks_cache["url"] = url or path
    return keys


def _signing_key(token: str):
    """Resolve the verification key for a token (single key or a key set)."""
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise AuthError(f"malformed token header: {exc}") from exc
    kid = header.get("kid")
    keys = _fetch_jwks().get("keys", [])
    if not keys:
        raise AuthError("JWKS contains no keys")
    if kid:
        for key in keys:
            if key.get("kid") == kid:
                return jwt.PyJWK(key).key
        # A rotated key: refresh once so a new kid does not lock users out.
        with _jwks_lock:
            _jwks_cache["fetched"] = 0.0
        keys = _fetch_jwks().get("keys", [])
        for key in keys:
            if key.get("kid") == kid:
                return jwt.PyJWK(key).key
        raise AuthError(f"no signing key for kid={kid}")
    if len(keys) == 1:
        return jwt.PyJWK(keys[0]).key
    raise AuthError("token has no kid but the JWKS has multiple keys")


def clear_jwks_cache() -> None:
    """Force the next verification to refetch keys (tests / key rotation)."""
    with _jwks_lock:
        _jwks_cache.update({"keys": None, "fetched": 0.0, "url": ""})


def _claim(claims: Dict[str, Any], path: str) -> Any:
    """Read a potentially dotted claim (`realm_access.roles`)."""
    if not path:
        return None
    current: Any = claims
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


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
    if _jwks_configured():
        return
    secret = (settings.AUTH_JWT_SECRET or "").strip()
    if len(secret) < 32:
        raise RuntimeError(
            "AUTH_MODE=jwt requires AUTH_JWT_SECRET (>=32 chars) or AUTH_JWKS_URL/FILE"
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
    algorithms = (
        [item.strip() for item in settings.AUTH_JWT_ALGORITHMS.split(",") if item.strip()]
        if _jwks_configured()
        else [settings.AUTH_JWT_ALGORITHM]
    )
    kwargs: dict = {"algorithms": algorithms, "options": options}
    if settings.AUTH_JWT_ISSUER:
        kwargs["issuer"] = settings.AUTH_JWT_ISSUER
    if settings.AUTH_JWT_AUDIENCE:
        kwargs["audience"] = settings.AUTH_JWT_AUDIENCE
    try:
        if _jwks_configured():
            claims = jwt.decode(token, _signing_key(token), **kwargs)
        else:
            claims = jwt.decode(token, _secret(), **kwargs)
    except AuthError:
        raise
    except Exception as exc:
        raise AuthError(str(exc)) from exc

    if now is not None and int(claims.get("exp", 0)) < int(now):
        raise AuthError("token expired")

    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        raise AuthError("token has no subject")

    roles = _clean_list(_claim(claims, settings.AUTH_CLAIM_ROLES))
    departments = _clean_list(_claim(claims, settings.AUTH_CLAIM_DEPTS))
    if settings.AUTH_ROLE_DEPT_PREFIX:
        prefix = settings.AUTH_ROLE_DEPT_PREFIX
        departments = tuple(
            dict.fromkeys(
                list(departments)
                + [role[len(prefix):] for role in roles if role.startswith(prefix)]
            )
        )
    admin = bool(_claim(claims, settings.AUTH_CLAIM_ADMIN))
    if settings.AUTH_ADMIN_ROLE:
        admin = admin or settings.AUTH_ADMIN_ROLE in roles

    return Identity(
        user_id=user_id,
        departments=departments,
        groups=_clean_list(_claim(claims, settings.AUTH_CLAIM_GROUPS)),
        is_admin=admin,
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
