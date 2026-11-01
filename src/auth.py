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
import re
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
    # Set only for tokens minted by the local login endpoint. Absent (None) means
    # the token came from an external issuer (gateway/IdP), which has its own
    # revocation mechanism, so the local users file is not consulted.
    token_version: Optional[int] = None


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
    if identity.token_version is not None:
        # Marks a locally issued token so the callback path can bind it to the
        # account version in AUTH_USERS_FILE (revocation on disable/password change).
        claims["ver"] = int(identity.token_version)
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
    deny = (settings.AUTH_DENY_ROLE or "").strip()
    if deny and deny in roles:
        raise AuthError(f"role {deny!r} is not allowed to use this service")
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

    if not departments:
        departments = _clean_list((settings.AUTH_DEFAULT_DEPTS or "").split(","))

    version = claims.get("ver")
    token_version: Optional[int] = None
    if version is not None:
        try:
            token_version = int(version)
        except (TypeError, ValueError) as exc:
            raise AuthError("token has an invalid version claim") from exc

    return Identity(
        user_id=user_id,
        departments=departments,
        groups=_clean_list(_claim(claims, settings.AUTH_CLAIM_GROUPS)),
        is_admin=admin,
        token_version=token_version,
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
    identity = decode_token(token)
    _check_local_account(identity)
    return identity


def _check_local_account(identity: Identity) -> None:
    """Enforce local-account revocation on tokens minted by /auth/login.

    Only tokens carrying a `ver` claim are bound to ``AUTH_USERS_FILE``: an IdP
    or gateway token has no such claim and keeps working unchanged, so enabling
    local login can never invalidate externally issued tokens.
    """
    if identity.token_version is None:
        return
    from src import local_auth

    try:
        local_auth.assert_user_active(identity.user_id, identity.token_version)
    except local_auth.LocalAuthError as exc:
        raise AuthError(str(exc)) from exc


def oidc_login_configured() -> bool:
    return bool(
        (settings.AUTH_OIDC_CLIENT_ID or "").strip()
        and (settings.AUTH_OIDC_CLIENT_SECRET or "").strip()
        and (settings.AUTH_OIDC_AUTHORIZE_URL or "").strip()
        and (settings.AUTH_OIDC_TOKEN_URL or "").strip()
        and (settings.AUTH_OIDC_REDIRECT_URI or "").strip()
    )


def build_authorize_url(state: str) -> str:
    """Authorization URL for the provider's login page."""
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": settings.AUTH_OIDC_CLIENT_ID,
        "redirect_uri": settings.AUTH_OIDC_REDIRECT_URI,
        "scope": settings.AUTH_OIDC_SCOPE,
        "state": state,
    }
    return f"{settings.AUTH_OIDC_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str) -> Dict[str, Any]:
    """Exchange an authorization code for tokens, then verify the id_token."""
    import requests

    try:
        response = requests.post(
            settings.AUTH_OIDC_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.AUTH_OIDC_CLIENT_ID,
                "client_secret": settings.AUTH_OIDC_CLIENT_SECRET,
                "redirect_uri": settings.AUTH_OIDC_REDIRECT_URI,
            },
            timeout=10,
        )
        if response.status_code >= 400:
            raise AuthError(f"token exchange failed: HTTP {response.status_code}")
        payload = response.json()
    except AuthError:
        raise
    except Exception as exc:
        raise AuthError(f"token exchange failed: {exc}") from exc
    id_token = payload.get("id_token")
    if not id_token:
        raise AuthError("provider response has no id_token")
    identity = decode_token(id_token)
    return {"identity": identity, "id_token": id_token, "raw": payload}


def apply_identity(identity: Optional[Identity], departments: List[str],
                   groups: List[str]) -> Tuple[List[str], List[str], str]:
    """Return the effective departments/groups/user for a request.

    With an identity, everything comes from the token and the request body is
    ignored: a caller cannot widen its own permissions by editing the payload.
    """
    if identity is None:
        return departments, groups, ""
    return list(identity.departments), list(identity.groups), identity.user_id


# --- auth routes (mounted by main.py) --------------------------------------

_NEXT_TARGET = re.compile(r"^/[^\s\\]*$")
_STATE_COOKIE = "agent_rag_oidc_state"
_STATE_TTL = 600


def _safe_next(target: str) -> str:
    """Only allow same-origin absolute paths (no scheme-relative ``//host``)."""
    target = (target or "").strip()
    # The callback appends its own #token= fragment; drop any pre-existing one.
    target = target.split("#", 1)[0]
    if not target.startswith("/") or target.startswith("//"):
        return "/admin"
    if not _NEXT_TARGET.fullmatch(target):
        return "/admin"
    return target


def _js_string(value: str) -> str:
    """Encode a Python string as a JS literal that is safe inside <script>."""
    encoded = json.dumps(value)
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("'", "\\u0027")
    )


def _sign_callback_token(identity: Identity) -> str:
    """Callback returns a token the console stores as its bearer credential."""
    if _jwks_configured():
        # With a JWKS the app cannot mint provider-signed tokens; hand back the
        # provider's id_token instead (handled by the caller).
        raise AuthError("token minting is unavailable in JWKS mode")
    return encode_token(identity, ttl_seconds=min(settings.AUTH_TOKEN_TTL, 3600))


def register_routes(app) -> None:
    """Add /auth/login and /auth/callback (only when an OIDC client is set)."""
    import base64
    import hashlib
    import hmac
    import secrets

    from fastapi import HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, RedirectResponse

    def _state_secret() -> bytes:
        # AUTH_JWT_SECRET is preferred; the client secret is a reasonable
        # fallback because it is required for the code flow anyway.
        raw = (settings.AUTH_JWT_SECRET or settings.AUTH_OIDC_CLIENT_SECRET or "").strip()
        return raw.encode()

    def _sign_state(value: str) -> str:
        # Base64url keeps the state cookie/query free of characters that would
        # need quoting (the payload contains ``|`` and a URL path).
        payload = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
        digest = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{digest}"

    def _verify_state(value: str) -> str:
        if "." not in value:
            raise HTTPException(status_code=400, detail="invalid state")
        payload, digest = value.rsplit(".", 1)
        expected = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise HTTPException(status_code=400, detail="invalid state")
        try:
            padded = payload + "=" * (-len(payload) % 4)
            return base64.urlsafe_b64decode(padded.encode()).decode()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid state")

    def _secure_request(request: Request) -> bool:
        # Behind a TLS-terminating proxy the scheme is only in X-Forwarded-Proto.
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        return proto.split(",", 1)[0].strip().lower() == "https"

    @app.get("/auth/login", include_in_schema=False)
    def auth_login(request: Request):
        if not auth_enabled() or not oidc_login_configured():
            raise HTTPException(status_code=404, detail="OIDC login is not configured")
        # The console returns to its own page with the token in the fragment.
        target = _safe_next(request.query_params.get("next", "/admin"))
        state = _sign_state(f"{secrets.token_urlsafe(16)}|{target}")
        response = RedirectResponse(build_authorize_url(state))
        # Bind the state to this browser: the callback must present the same
        # value, which blocks login-CSRF (an attacker's code replayed into a
        # victim's browser). SameSite=Lax survives the provider redirect.
        response.set_cookie(
            _STATE_COOKIE,
            state,
            max_age=_STATE_TTL,
            httponly=True,
            samesite="lax",
            secure=_secure_request(request),
            path="/auth",
        )
        return response

    @app.get("/auth/callback", include_in_schema=False)
    def auth_callback(
        request: Request,
        code: str = Query(""),
        state: str = Query(""),
    ):
        if not auth_enabled() or not oidc_login_configured():
            raise HTTPException(status_code=404, detail="OIDC login is not configured")
        raw = _verify_state(state)
        bound = request.cookies.get(_STATE_COOKIE, "")
        if not bound or not hmac.compare_digest(bound, state):
            raise HTTPException(status_code=400, detail="invalid state")
        target = _safe_next(raw.split("|", 1)[1] if "|" in raw else "/admin")
        if not code:
            raise HTTPException(status_code=400, detail="missing authorization code")
        try:
            result = exchange_code(code)
        except AuthError as exc:
            logger.warning("OIDC callback failed: %s", exc)
            raise HTTPException(status_code=401, detail="login failed")

        identity: Identity = result["identity"]
        # In JWKS mode the provider id_token is itself a valid bearer token.
        token = result["id_token"] if _jwks_configured() else _sign_callback_token(identity)

        html = (
            "<!doctype html><meta charset=utf-8><title>登录成功</title>"
            "<script>"
            f"location.replace({_js_string(target)} + '#token=' + {_js_string(token)});"
            "</script>"
            "<p>登录成功，正在返回管理台…</p>"
        )
        response = HTMLResponse(html, headers={"Cache-Control": "no-store"})
        response.delete_cookie(_STATE_COOKIE, path="/auth")
        return response
