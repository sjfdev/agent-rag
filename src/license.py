"""Offline license verification for private deployments.

The license is a signed JSON document kept next to the deployment (by default
``config/license.json``); verifying it needs no network call and no secret at
rest on the customer machine. Only the vendor's Ed25519 **public** key ships in
this file, the private key stays offline and signs licenses with
``scripts/issue_license.py``.

    {
      "payload": {
        "v": 1,
        "license_id": "LIC-2026-001",
        "licensee": "ACME Corp",
        "edition": "enterprise",
        "issued_at": "2026-11-17",
        "expires_at": "2027-11-17",
        "max_depts": 5,
        "max_docs": 100000,
        "features": ["sso", "audit", "monitoring"]
      },
      "signature": "<base64 Ed25519 signature over canonical payload>"
    }

``payload`` is serialised as canonical JSON (sorted keys, no whitespace,
UTF-8) before signing, so re-indenting the file does not break verification.

Enforcement is deliberately soft: a missing/invalid license refuses startup
(fail closed, like the document ACL), while an **expired** license keeps
serving chat and citations and only blocks admin writes (ingestion, uploads,
model changes). A customer never loses access to their own knowledge base
because a renewal slipped. ``LICENSE_ENABLED=false`` (the default) leaves the
whole mechanism inert.

This is a contract anchor, not DRM: the source is MIT-licensed and a customer
with shell access can always disable the check. That is why the license also
carries limits (departments/documents) and edition/features: it documents what
was sold and gives the console and ``scripts/doctor.py`` something to report.
"""

import base64
import json
import logging
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from src.config import settings

logger = logging.getLogger("agent-rag.license")

PUBLIC_KEY_B64 = "WuRyhixhR8+5/+DM8X1fAHo3d8euIGyR5pKSOXVL73A="

LICENSE_VERSION = 1
VALID_STATES = ("disabled", "valid", "grace", "expired", "missing", "invalid")
WRITE_STATES = ("disabled", "valid", "grace")
# Evaluation licenses: 30 days by default, never longer than 90. They carry the
# same limits/features as a paid license so a prospect can test the real thing;
# the short expiry and the console "trial" badge are the commercial boundary.
TRIAL_EDITION = "trial"
TRIAL_WARN_DAYS = 7
MAX_TRIAL_DAYS = 90

_lock = threading.Lock()
_cache: Dict[str, Any] = {"key": None, "payload": None, "error": None}


class LicenseError(Exception):
    """Raised for a malformed, unsigned or wrongly signed license."""


# --- key handling ----------------------------------------------------------


def _public_key(value: str = "") -> Ed25519PublicKey:
    raw = (value or settings.LICENSE_PUBLIC_KEY or PUBLIC_KEY_B64).strip()
    if not raw:
        raise LicenseError("no license public key configured")
    try:
        if raw.startswith("-----BEGIN"):
            key = serialization.load_pem_public_key(raw.encode("utf-8"))
            if not isinstance(key, Ed25519PublicKey):
                raise LicenseError("public key is not Ed25519")
            return key
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(raw, validate=True))
    except LicenseError:
        raise
    except Exception as exc:  # ValueError, binascii.Error, ...
        raise LicenseError(f"invalid license public key: {type(exc).__name__}") from exc


def canonical_payload(payload: Dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# --- parsing / verification ------------------------------------------------


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or len(value) < 10:
        raise LicenseError(f"{field} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise LicenseError(f"{field} is not a valid date: {value}") from exc


def _validate_payload(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise LicenseError("license payload must be an object")
    if payload.get("v") != LICENSE_VERSION:
        raise LicenseError(f"unsupported license version: {payload.get('v')!r}")
    licensee = payload.get("licensee")
    if not isinstance(licensee, str) or not licensee.strip():
        raise LicenseError("licensee is required")
    issued = _parse_date(payload.get("issued_at"), "issued_at")
    expires = _parse_date(payload.get("expires_at"), "expires_at")
    if expires < issued:
        raise LicenseError("expires_at is before issued_at")
    if str(payload.get("edition", "")).strip().lower() == TRIAL_EDITION:
        if (expires - issued).days > MAX_TRIAL_DAYS:
            raise LicenseError(
                f"trial licenses cannot exceed {MAX_TRIAL_DAYS} days"
            )
    not_before = payload.get("not_before")
    if not_before is not None and _parse_date(not_before, "not_before") > expires:
        raise LicenseError("not_before is after expires_at")
    for field in ("max_depts", "max_docs"):
        value = payload.get(field, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LicenseError(f"{field} must be an integer >= 0")
    features = payload.get("features", [])
    if not isinstance(features, list) or not all(
        isinstance(item, str) for item in features
    ):
        raise LicenseError("features must be a list of strings")


def verify_blob(blob: Any, public_key: str = "") -> Dict[str, Any]:
    """Return the signed payload or raise :class:`LicenseError`."""
    if not isinstance(blob, dict):
        raise LicenseError("license file must contain a JSON object")
    payload = blob.get("payload")
    signature = blob.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise LicenseError("license needs payload and signature fields")
    _validate_payload(payload)
    try:
        raw_signature = base64.b64decode(signature, validate=True)
    except Exception as exc:
        raise LicenseError("signature is not valid base64") from exc
    try:
        _public_key(public_key).verify(raw_signature, canonical_payload(payload))
    except InvalidSignature as exc:
        raise LicenseError("signature does not match this license") from exc
    return payload


def verify_file(path: str, public_key: str = "") -> Dict[str, Any]:
    file = Path(path)
    try:
        blob = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LicenseError(f"license file not found: {file}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LicenseError(f"cannot read license file: {type(exc).__name__}") from exc
    return verify_blob(blob, public_key)


def _load_payload() -> Dict[str, Any]:
    """Parse and verify the configured file, cached by path + mtime + key."""
    file = Path(settings.LICENSE_FILE)
    try:
        stat = file.stat()
    except OSError:
        return {"payload": None, "error": f"license file not found: {file}"}
    key = (
        str(file),
        stat.st_mtime_ns,
        stat.st_size,
        settings.LICENSE_PUBLIC_KEY or PUBLIC_KEY_B64,
    )
    with _lock:
        if _cache["key"] == key:
            return {"payload": _cache["payload"], "error": _cache["error"]}
    try:
        payload = verify_file(str(file))
        error = None
    except LicenseError as exc:
        payload, error = None, str(exc)
    with _lock:
        _cache.update(key=key, payload=payload, error=error)
    return {"payload": payload, "error": error}


def reset_cache() -> None:
    with _lock:
        _cache.update(key=None, payload=None, error=None)


# --- status ----------------------------------------------------------------


def _today() -> date:
    return datetime.now(timezone.utc).date()


def status(now: Optional[date] = None) -> Dict[str, Any]:
    """Describe the current license; ``ok`` says whether admin writes are allowed."""
    grace_days = max(0, int(settings.LICENSE_GRACE_DAYS))
    base: Dict[str, Any] = {
        "enabled": bool(settings.LICENSE_ENABLED),
        "state": "disabled",
        "ok": True,
        "edition": None,
        "trial": False,
        "licensee": None,
        "license_id": None,
        "features": [],
        "max_depts": None,
        "max_docs": None,
        "issued_at": None,
        "not_before": None,
        "expires_at": None,
        "days_left": None,
        "grace_days": grace_days,
        "warn": False,
        "error": None,
        "file": str(settings.LICENSE_FILE),
    }
    if not settings.LICENSE_ENABLED:
        return base

    loaded = _load_payload()
    payload = loaded["payload"]
    if payload is None:
        base["state"] = "missing" if "not found" in (loaded["error"] or "") else "invalid"
        base["ok"] = False
        base["error"] = loaded["error"]
        return base

    today = now or _today()
    expires = _parse_date(payload["expires_at"], "expires_at")
    not_before = payload.get("not_before")
    days_left = (expires - today).days
    trial = str(payload.get("edition", "")).strip().lower() == TRIAL_EDITION
    base.update(
        state="valid",
        ok=True,
        edition=payload.get("edition", "standard"),
        trial=trial,
        licensee=payload["licensee"],
        license_id=payload.get("license_id"),
        features=list(payload.get("features", [])),
        max_depts=payload.get("max_depts", 0),
        max_docs=payload.get("max_docs", 0),
        issued_at=payload.get("issued_at"),
        not_before=not_before,
        expires_at=payload.get("expires_at"),
        days_left=days_left,
    )
    if not_before and today < _parse_date(not_before, "not_before"):
        base.update(state="invalid", ok=False, error="license is not valid yet")
        return base
    if days_left < 0:
        if -days_left <= grace_days:
            base.update(state="grace", ok=True)
            return base
        base.update(
            state="expired",
            ok=False,
            error=(
                f"license expired {abs(days_left)} day(s) ago; "
                "admin writes are blocked until it is renewed"
            ),
        )
        return base
    warn_days = TRIAL_WARN_DAYS if trial else max(0, int(settings.LICENSE_WARN_DAYS))
    base["warn"] = days_left <= warn_days
    return base


def writes_allowed() -> bool:
    return bool(status()["ok"])


def describe() -> Dict[str, Any]:
    """Subset safe for ``/info`` (no file paths)."""
    described = dict(status())
    described.pop("file", None)
    return described


def validate_config() -> None:
    """Fail closed on a missing/bad license; warn (do not raise) on expiry."""
    if not settings.LICENSE_ENABLED:
        return
    state = status()
    if state["state"] in ("missing", "invalid"):
        raise RuntimeError(f"license misconfigured: {state['error']}")
    if state["state"] == "expired":
        logger.warning("license expired: %s (chat stays up, admin writes blocked)", state["error"])
    elif state["state"] == "grace":
        logger.warning(
            "license expired %d day(s) ago, running in grace period (%d day(s) left)",
            abs(state["days_left"]),
            state["grace_days"] + state["days_left"],
        )
    elif state.get("trial"):
        logger.warning(
            "trial license for %s: %d day(s) left, evaluation only",
            state["licensee"],
            state["days_left"],
        )
    elif state["warn"]:
        logger.warning("license expires in %d day(s)", state["days_left"])


def load_private_key(path: str) -> Ed25519PrivateKey:
    """Vendor-side helper used by ``scripts/issue_license.py`` (never on the server)."""
    try:
        key = serialization.load_pem_private_key(
            Path(path).read_bytes(), password=None
        )
    except FileNotFoundError as exc:
        raise LicenseError(f"private key not found: {path}") from exc
    except Exception as exc:
        raise LicenseError(f"cannot read private key: {type(exc).__name__}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise LicenseError("private key is not Ed25519")
    return key


def sign_payload(payload: Dict[str, Any], private_key: Ed25519PrivateKey) -> Dict[str, Any]:
    _validate_payload(payload)
    signature = private_key.sign(canonical_payload(payload))
    return {"payload": payload, "signature": base64.b64encode(signature).decode("ascii")}


def install(blob: Any, path: Optional[str] = None) -> Dict[str, Any]:
    """Verify and atomically persist a license, then return the new status.

    The signature is checked against the vendor public key *before* anything is
    written, so a typo or a tampered file can never replace a working license.
    The file is written 0600 because it names the customer and their limits.
    """
    payload = verify_blob(blob)
    target = Path(path or settings.LICENSE_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(blob, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    with _lock:
        os.replace(tmp, target)
        _cache.update(key=None, payload=None, error=None)
    return {"license": status(), "payload": payload}
