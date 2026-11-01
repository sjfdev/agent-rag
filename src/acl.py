"""Optional document-level ACL enforced at retrieval time.

A JSON file maps a source (``dept/path`` or a bare path) to the user ids and
groups allowed to see it. Documents without an entry are public inside their
department, so enabling ACLs never hides existing content by accident.

Format::

    {
      "Finance/confidential_budget_2026.pdf": ["u_finance", "group:exec"],
      "HR/salaries.xlsx": ["group:hr"]
    }

The file is re-read when its mtime changes, so edits apply without a restart.
This is an application-level check: it runs after retrieval for both backends,
which keeps it backend-agnostic (no schema change / re-ingestion required).

Failure policy: **fail closed**. When ``ACL_ENABLED`` is true but the file is
missing, unreadable or not valid JSON, no document with an ACL rule is served
(and ``validate_config`` refuses to start the app). Silently treating a broken
ACL file as "no restrictions" would hand out protected documents after a typo
or a forgotten mount.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from src.config import settings

logger = logging.getLogger("agent-rag.acl")

_lock = threading.Lock()
_state = {"mtime": None, "rules": {}, "error": None}


class ACLConfigError(RuntimeError):
    """ACL_ENABLED is true but the rules file cannot be used."""


def _read(path: Path) -> Dict[str, List[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ACLConfigError(f"cannot read ACL file {path}: {exc}") from exc
    except ValueError as exc:
        raise ACLConfigError(f"ACL file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ACLConfigError(f"ACL file {path} must contain a JSON object")

    rules: Dict[str, List[str]] = {}
    for source, allowed in raw.items():
        if isinstance(allowed, str):
            allowed = [allowed]
        if isinstance(allowed, list):
            rules[str(source)] = [str(item) for item in allowed]
        else:
            raise ACLConfigError(
                f"ACL rule for {source!r} must be a list of user ids / groups"
            )
    return rules


def _load() -> Dict[str, List[str]]:
    """Return the cached rules, re-reading the file when it changed.

    Raises ACLConfigError when ACLs are enabled and the file is unusable.
    """
    path = Path(settings.ACL_FILE)
    if not path.is_file():
        raise ACLConfigError(f"ACL_ENABLED=true but {path} does not exist")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise ACLConfigError(f"cannot stat ACL file {path}: {exc}") from exc

    with _lock:
        if _state["mtime"] == mtime and _state["error"] is None:
            return _state["rules"]
        if _state["error"]:
            # Only log the transition, not once per retrieved document.
            logger.error("ACL still unusable: %s", _state["error"])
        try:
            rules = _read(path)
        except ACLConfigError as exc:
            _state.update({"mtime": mtime, "rules": {}, "error": str(exc)})
            logger.error("refusing to serve ACL-protected documents: %s", exc)
            raise
        _state.update({"mtime": mtime, "rules": rules, "error": None})
        logger.info("loaded %d ACL rule(s) from %s", len(rules), path)
        return rules


def validate_config() -> None:
    """Fail fast when ACLs are enabled but the rules file is unusable."""
    if not settings.ACL_ENABLED:
        return
    _load()


def status() -> Dict[str, object]:
    """Non-raising snapshot for /health and scripts/doctor.py."""
    if not settings.ACL_ENABLED:
        return {"enabled": False, "ok": True, "rules": 0, "error": None}
    try:
        rules = _load()
    except ACLConfigError as exc:
        return {"enabled": True, "ok": False, "rules": 0, "error": str(exc)}
    return {"enabled": True, "ok": True, "rules": len(rules), "error": None}


def refresh() -> None:
    """Force the next check to reload the file (tests)."""
    with _lock:
        _state.update({"mtime": None, "rules": {}, "error": None})


def allowed_groups(user_id: str, groups: Optional[Iterable[str]] = None) -> set:
    """Identities that may match an ACL rule: the user id plus group tags."""
    identities = {user_id}
    for group in groups or []:
        identities.add(group if str(group).startswith("group:") else f"group:{group}")
    return identities


def is_allowed(source: str, user_id: Optional[str], groups: Optional[Iterable[str]] = None) -> bool:
    """True unless an ACL rule exists and none of the identities match.

    Fails closed: when the rules cannot be loaded while ACLs are enabled, the
    document is treated as restricted.
    """
    if not settings.ACL_ENABLED:
        return True
    try:
        rules = _load()
    except ACLConfigError:
        return False
    if not rules or not source:
        return True
    rule = rules.get(source)
    if rule is None:
        # Also accept a rule keyed by the bare path without the department.
        rule = rules.get(source.split("/", 1)[-1])
    if rule is None:
        return True
    if not user_id:
        return False
    return bool(allowed_groups(user_id, groups) & set(rule))


def filter_documents(docs, user_id: Optional[str], groups: Optional[Iterable[str]] = None):
    """Drop documents the user may not see.

    A broken ACL file drops everything rather than everything-restricted: the
    alternative (returning the unfiltered list) is a data leak.
    """
    if not settings.ACL_ENABLED or not docs:
        return docs
    return [
        doc
        for doc in docs
        if is_allowed((doc.metadata or {}).get("source") or "", user_id, groups)
    ]
