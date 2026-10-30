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
"""

import json
import logging
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from src.config import settings

logger = logging.getLogger("agent-rag.acl")

_lock = threading.Lock()
_state = {"mtime": None, "rules": {}}


def _load() -> Dict[str, List[str]]:
    path = Path(settings.ACL_FILE)
    if not path.is_file():
        return {}
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    with _lock:
        if _state["mtime"] == mtime:
            return _state["rules"]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("could not parse ACL file %s", path, exc_info=True)
            return _state["rules"]
        rules: Dict[str, List[str]] = {}
        if isinstance(raw, dict):
            for source, allowed in raw.items():
                if isinstance(allowed, str):
                    allowed = [allowed]
                if isinstance(allowed, list):
                    rules[str(source)] = [str(item) for item in allowed]
        _state["mtime"] = mtime
        _state["rules"] = rules
        logger.info("loaded %d ACL rule(s) from %s", len(rules), path)
        return rules


def refresh() -> None:
    """Force the next check to reload the file (tests)."""
    with _lock:
        _state["mtime"] = None
        _state["rules"] = {}


def allowed_groups(user_id: str, groups: Optional[Iterable[str]] = None) -> set:
    """Identities that may match an ACL rule: the user id plus group tags."""
    identities = {user_id}
    for group in groups or []:
        identities.add(group if str(group).startswith("group:") else f"group:{group}")
    return identities


def is_allowed(source: str, user_id: Optional[str], groups: Optional[Iterable[str]] = None) -> bool:
    """True unless an ACL rule exists and none of the identities match."""
    if not settings.ACL_ENABLED:
        return True
    rules = _load()
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
    """Drop documents the user may not see."""
    if not settings.ACL_ENABLED or not docs:
        return docs
    kept = []
    for doc in docs:
        source = (doc.metadata or {}).get("source") or ""
        if is_allowed(source, user_id, groups):
            kept.append(doc)
    return kept
