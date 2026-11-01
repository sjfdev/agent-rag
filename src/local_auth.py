"""Local username/password accounts (no external IdP).

Purpose
-------
`AUTH_MODE=jwt` trusts a signed bearer token, but something has to *issue* one.
Production deployments hand that job to an IdP (`AUTH_OIDC_*`, verified with
JWKS). This module covers the offline case: accounts are stored in a JSON file,
passwords are hashed with `hashlib.scrypt` (standard library only, ~150 ms per
verification) and a successful login mints exactly the same HS256 bearer token
the rest of the app already understands.

Nothing downstream changes: departments/groups/admin come from the token, the
cache scope is still derived from the token, and the audit log records
`user:<sub>`. Enabling local login cannot widen anyone's permissions.

File format
-----------
``AUTH_USERS_FILE`` is a JSON object keyed by username::

    {
      "version": 1,
      "users": {
        "alice": {
          "password": "scrypt$16384$8$1$<salt-b64>$<hash-b64>",
          "depts": ["Tech", "HR"],
          "groups": ["exec"],
          "admin": true,
          "disabled": false,
          "token_version": 1,
          "created": "2026-11-01T00:00:00+00:00",
          "updated": "..."
        }
      }
    }

`token_version` is the revocation knob: bumping it (password change, admin
"force logout") invalidates every token minted for the old version, because the
token carries a matching `ver` claim that `auth.decode_token` compares against
the file. `disabled` blocks login and token use immediately.

Writes are atomic (temp file + `os.replace`) and serialised with a lock, so a
crash never leaves a half-written user database.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings
from src.validation import InvalidDepartment, validate_dept

logger = logging.getLogger("agent-rag.local_auth")

# Deliberately CPU-heavy: ~150 ms on the reference machine. n=2**14 keeps
# verification fast enough for interactive logins while making offline
# brute-force expensive.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
USERNAME_RE_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@"
)
MAX_USERNAME_LENGTH = 64
MAX_DEPTS = 20
MAX_GROUPS = 50

_lock = threading.RLock()
_cache: Dict[str, Any] = {"mtime": None, "data": None, "path": ""}


class LocalAuthError(Exception):
    """Raised when an account operation is rejected (invalid input, conflict)."""


# --- password hashing ------------------------------------------------------


def validate_password(password: str) -> None:
    """Reject passwords that are too short, too long or not a string."""
    if not isinstance(password, str):
        raise LocalAuthError("password must be a string")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise LocalAuthError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise LocalAuthError(
            f"password must be at most {MAX_PASSWORD_LENGTH} characters"
        )


def hash_password(password: str) -> str:
    """Hash a password with scrypt; the result is self-describing."""
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a password against a stored scrypt hash."""
    if not password or not encoded:
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        # Cap the work factor we are willing to honour so a tampered file
        # cannot turn a login into a multi-second CPU stall.
        if int(n) > SCRYPT_N * 4 or int(r) > SCRYPT_R * 4 or int(p) > SCRYPT_P * 4:
            logger.warning("refusing excessive scrypt parameters in users file")
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# --- user database ---------------------------------------------------------


def _path() -> Path:
    return Path(settings.AUTH_USERS_FILE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_db() -> Dict[str, Any]:
    return {"version": 1, "users": {}}


def _read_file() -> Dict[str, Any]:
    path = _path()
    if not path.is_file():
        return _empty_db()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A corrupt file must not silently wipe accounts, but it also must not
        # crash the process: log loudly and treat it as "no accounts" so the
        # rest of the app (chat, admin) keeps working.
        logger.error("cannot read users file %s: %s", path, exc)
        return _empty_db()
    if not isinstance(raw, dict):
        logger.error("users file %s is not a JSON object", path)
        return _empty_db()
    users = raw.get("users")
    if not isinstance(users, dict):
        raw["users"] = {}
    raw.setdefault("version", 1)
    return raw


def _load() -> Dict[str, Any]:
    """Cached read keyed by mtime, so CLI edits apply without a restart."""
    path = _path()
    try:
        mtime = path.stat().st_mtime_ns if path.exists() else None
    except OSError:
        mtime = None
    with _lock:
        if _cache["path"] == str(path) and _cache["mtime"] == mtime and _cache["data"]:
            return _cache["data"]
        data = _read_file()
        _cache.update({"mtime": mtime, "data": data, "path": str(path)})
        return data


def _save(data: Dict[str, Any]) -> None:
    """Atomic write: a crash leaves either the old or the new file."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    tmp = path.with_name(path.name + ".tmp")
    with _lock:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            _cache.update(
                {"mtime": path.stat().st_mtime_ns, "data": data, "path": str(path)}
            )
        except OSError:
            _cache.update({"mtime": None, "data": None, "path": ""})


def clear_cache() -> None:
    """Force a re-read on the next call (tests, or after an external edit)."""
    with _lock:
        _cache.update({"mtime": None, "data": None, "path": ""})


def validate_username(username: str) -> str:
    name = (username or "").strip()
    if not name or len(name) > MAX_USERNAME_LENGTH:
        raise LocalAuthError("invalid username")
    if not set(name) <= USERNAME_RE_ALLOWED:
        raise LocalAuthError(
            "username may only contain letters, digits and . _ - @"
        )
    return name


def _clean_depts(depts: Any) -> List[str]:
    if depts is None:
        depts = []
    if isinstance(depts, str):
        depts = [depts]
    if not isinstance(depts, (list, tuple)):
        raise LocalAuthError("depts must be a list")
    if len(depts) > MAX_DEPTS:
        raise LocalAuthError(f"at most {MAX_DEPTS} departments are allowed")
    cleaned: List[str] = []
    for dept in depts:
        try:
            # validate_dept also escapes the value for Milvus expressions; the
            # escaped form is what the token must carry.
            cleaned.append(validate_dept(str(dept).strip()))
        except InvalidDepartment as exc:
            raise LocalAuthError(f"invalid department: {dept}") from exc
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned and not (settings.AUTH_DEFAULT_DEPTS or "").strip():
        # A token with no departments is rejected by /chat with a confusing
        # "invalid department name". Catch it when the account is created or
        # changed instead, so the operator fixes it here.
        raise LocalAuthError(
            "at least one department is required "
            "(or set AUTH_DEFAULT_DEPTS to grant one to every account)"
        )
    return cleaned


def _clean_groups(groups: Any) -> List[str]:
    if groups is None:
        return []
    if isinstance(groups, str):
        groups = [groups]
    if not isinstance(groups, (list, tuple)):
        raise LocalAuthError("groups must be a list")
    if len(groups) > MAX_GROUPS:
        raise LocalAuthError(f"at most {MAX_GROUPS} groups are allowed")
    cleaned = [str(item).strip() for item in groups]
    return list(dict.fromkeys(item for item in cleaned if item))


def _public(record: Dict[str, Any]) -> Dict[str, Any]:
    """A user record without the password hash (safe to send to the console)."""
    return {
        "username": record.get("username", ""),
        "depts": list(record.get("depts") or []),
        "groups": list(record.get("groups") or []),
        "admin": bool(record.get("admin")),
        "disabled": bool(record.get("disabled")),
        "token_version": int(record.get("token_version") or 1),
        "created": record.get("created") or "",
        "updated": record.get("updated") or "",
    }


def get_user(username: str) -> Optional[Dict[str, Any]]:
    """Raw record (contains the password hash) or None."""
    name = (username or "").strip()
    if not name:
        return None
    record = _load()["users"].get(name)
    if not isinstance(record, dict):
        return None
    record = dict(record)
    record["username"] = name
    return record


def list_users() -> List[Dict[str, Any]]:
    """Public view of every account, sorted by username."""
    users = _load()["users"]
    return [
        _public(dict(record, username=name))
        for name, record in sorted(users.items())
        if isinstance(record, dict)
    ]


def get_user_public(username: str) -> Optional[Dict[str, Any]]:
    """Public view of one account (no password hash), or None."""
    record = get_user(username)
    return _public(record) if record else None


def user_count() -> int:
    return len(_load()["users"])


def enabled() -> bool:
    """Local login is opt-in and only meaningful together with jwt mode."""
    return bool(settings.AUTH_LOCAL_LOGIN_ENABLED) and settings.AUTH_MODE == "jwt"


def create_user(
    username: str,
    password: str,
    *,
    depts: Any = None,
    groups: Any = None,
    admin: bool = False,
) -> Dict[str, Any]:
    """Create an account. Raises LocalAuthError on any validation problem."""
    name = validate_username(username)
    record = {
        "password": hash_password(password),
        "depts": _clean_depts(depts),
        "groups": _clean_groups(groups),
        "admin": bool(admin),
        "disabled": False,
        "token_version": 1,
        "created": _now(),
        "updated": _now(),
    }
    with _lock:
        data = _load()
        if name in data["users"]:
            raise LocalAuthError(f"user {name!r} already exists")
        data["users"][name] = record
        _save(data)
    logger.info("local account created: %s admin=%s", name, record["admin"])
    return _public(dict(record, username=name))


def validate_update(**changes: Any) -> None:
    """Dry-run the validation of `update_user` without touching the file.

    The admin endpoint applies a password change and a permission change in the
    same request; validating first means a rejected department name cannot leave
    the password already rotated.
    """
    allowed = {"depts", "groups", "admin", "disabled"}
    unknown = set(changes) - allowed
    if unknown:
        raise LocalAuthError(f"cannot update: {', '.join(sorted(unknown))}")
    if "depts" in changes:
        _clean_depts(changes["depts"])
    if "groups" in changes:
        _clean_groups(changes["groups"])
    if "disabled" in changes:
        bool(changes["disabled"])
    if "admin" in changes:
        bool(changes["admin"])


def update_user(username: str, **changes: Any) -> Dict[str, Any]:
    """Update departments/groups/admin/disabled. Bumps token_version on change."""
    name = validate_username(username)
    allowed = {"depts", "groups", "admin", "disabled"}
    unknown = set(changes) - allowed
    if unknown:
        raise LocalAuthError(f"cannot update: {', '.join(sorted(unknown))}")
    validate_update(**changes)
    with _lock:
        data = _load()
        record = data["users"].get(name)
        if not isinstance(record, dict):
            raise LocalAuthError(f"user {name!r} does not exist")
        record = dict(record)
        before = _public(dict(record, username=name))
        if "depts" in changes:
            record["depts"] = _clean_depts(changes["depts"])
        if "groups" in changes:
            record["groups"] = _clean_groups(changes["groups"])
        if "admin" in changes:
            record["admin"] = bool(changes["admin"])
        if "disabled" in changes:
            record["disabled"] = bool(changes["disabled"])
        after = _public(dict(record, username=name))
        if before != after:
            # Any permission change invalidates previously issued tokens.
            record["token_version"] = int(record.get("token_version") or 1) + 1
            record["updated"] = _now()
            data["users"][name] = record
            _save(data)
    return _public(dict(record, username=name))


def set_password(username: str, password: str) -> Dict[str, Any]:
    """Change a password and revoke every token minted before the change."""
    name = validate_username(username)
    encoded = hash_password(password)
    with _lock:
        data = _load()
        record = data["users"].get(name)
        if not isinstance(record, dict):
            raise LocalAuthError(f"user {name!r} does not exist")
        record = dict(record)
        record["password"] = encoded
        record["token_version"] = int(record.get("token_version") or 1) + 1
        record["updated"] = _now()
        data["users"][name] = record
        _save(data)
    logger.info("local password changed: %s", name)
    return _public(dict(record, username=name))


def delete_user(username: str) -> bool:
    """Remove an account. Returns False when it did not exist."""
    name = validate_username(username)
    with _lock:
        data = _load()
        if name not in data["users"]:
            return False
        del data["users"][name]
        _save(data)
    logger.info("local account deleted: %s", name)
    return True


def admin_count() -> int:
    return sum(
        1
        for record in _load()["users"].values()
        if isinstance(record, dict)
        and record.get("admin")
        and not record.get("disabled")
    )


_dummy_lock = threading.Lock()
_dummy_hash = ""


def _dummy_password_hash() -> str:
    """A real hash used to spend the same time on unknown users."""
    global _dummy_hash
    with _dummy_lock:
        if not _dummy_hash:
            _dummy_hash = hash_password(secrets.token_urlsafe(32))
        return _dummy_hash


def authenticate(
    username: str, password: str
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Check credentials.

    Returns ``(record, "")`` on success or ``(None, reason)`` on failure. The
    reason is for the audit log only; callers must answer with a generic message
    so the endpoint does not reveal which usernames exist.
    """
    name = (username or "").strip()
    if not name or not password:
        return None, "missing credentials"
    record = get_user(name)
    if record is None:
        # Spend comparable time so response timing does not enumerate users.
        verify_password(password, _dummy_password_hash())
        return None, "unknown user"
    if record.get("disabled"):
        return None, "account disabled"
    if not verify_password(password, str(record.get("password") or "")):
        return None, "bad password"
    return record, ""


def token_version_of(username: str) -> Optional[int]:
    """Current revocation version, or None when the account disappeared."""
    record = get_user(username)
    if record is None:
        return None
    return int(record.get("token_version") or 1)


def assert_user_active(username: str, version: int) -> None:
    """Raise LocalAuthError when a token belongs to a stale/disabled account."""
    record = get_user(username)
    if record is None:
        raise LocalAuthError("account no longer exists")
    if record.get("disabled"):
        raise LocalAuthError("account disabled")
    if int(record.get("token_version") or 1) != int(version):
        raise LocalAuthError("token has been revoked")


def stats() -> Dict[str, Any]:
    users = list_users()
    return {
        "enabled": enabled(),
        "path": str(_path()),
        "exists": _path().is_file(),
        "count": len(users),
        "admins": sum(1 for user in users if user["admin"] and not user["disabled"]),
    }


# --- login endpoint --------------------------------------------------------

_limiter_lock = threading.Lock()
_limiter = None
_limiter_config = None


def login_limiter():
    """Per-IP limiter for login attempts (a slow hash is also a CPU amplifier)."""
    global _limiter, _limiter_config
    from src.ratelimit import RateLimiter

    config = (int(settings.AUTH_LOGIN_RATE_LIMIT or 0), 60.0)
    with _limiter_lock:
        if _limiter is None or _limiter_config != config:
            _limiter = RateLimiter(limit=config[0], window=config[1])
            _limiter_config = config
        return _limiter


def reset_login_limiter() -> None:
    """Drop the limiter state (tests, or after a config change)."""
    global _limiter, _limiter_config
    with _limiter_lock:
        _limiter = None
        _limiter_config = None


def client_ip(headers: Any, fallback: str = "") -> str:
    """Best-effort caller address, preferring proxy headers."""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    forwarded = lowered.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or lowered.get("x-real-ip", "").strip() or fallback or "unknown"


def register_login_route(app) -> None:
    """Add ``POST /auth/login`` for username/password accounts.

    Returns JSON (``{"token": ...}``) so both consoles can store the bearer
    token the same way they store one pasted by hand. ``GET /auth/login`` stays
    reserved for the OIDC redirect, so the two login styles can coexist.
    """
    from fastapi import HTTPException, Request

    @app.post("/auth/login", include_in_schema=False)
    async def local_login(request: Request):
        if not enabled():
            raise HTTPException(status_code=404, detail="local login is not configured")

        payload = await _request_payload(request)
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")

        ip = client_ip(request.headers, request.client.host if request.client else "")
        allowed, retry_after = login_limiter().allow(f"ip:{ip}")
        if not allowed:
            logger.warning("login rate limited for %s", ip)
            raise HTTPException(
                status_code=429,
                detail="too many login attempts",
                headers={"Retry-After": str(int(max(retry_after, 1)))},
            )

        record, reason = authenticate(username, password)
        if record is None:
            # One generic message: never reveal whether the username exists.
            _audit_login(username, ip, False, reason)
            logger.warning("login failed user=%r ip=%s reason=%s", username, ip, reason)
            raise HTTPException(
                status_code=401,
                detail="invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

        name = record["username"]
        ttl = max(int(settings.AUTH_LOCAL_TOKEN_TTL or 0), 60)

        from src import auth

        identity = auth.Identity(
            user_id=name,
            departments=tuple(record.get("depts") or ()),
            groups=tuple(record.get("groups") or ()),
            is_admin=bool(record.get("admin")),
            # Binds the token to this account version so disable/password change
            # revokes it immediately.
            token_version=int(record.get("token_version") or 1),
        )
        token = auth.encode_token(identity, ttl_seconds=ttl)
        _audit_login(name, ip, True, "")
        logger.info("login ok user=%s ip=%s admin=%s", name, ip, identity.is_admin)
        return {
            "token": token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "user": _public(record),
        }


async def _request_payload(request) -> Dict[str, Any]:
    """Accept JSON and classic form posts from the same endpoint."""
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}
    try:
        form = await request.form()
    except Exception:
        return {}
    return {key: form[key] for key in form}


def _audit_login(username: str, ip: str, ok: bool, reason: str) -> None:
    """Record login outcomes; never raises (audit is best-effort)."""
    try:
        from src import audit

        audit.log_event(
            "auth.login",
            f"user:{username}" if ok else f"anonymous:{ip}",
            ok=ok,
            detail={"ip": ip, "reason": reason} if not ok else {"ip": ip},
        )
    except Exception:  # pragma: no cover - never break login on audit failure
        logger.warning("could not write login audit record", exc_info=True)

