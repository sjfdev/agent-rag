"""Append-only audit log for administrative actions.

One JSON object per line, so it can be tailed, grepped or shipped as-is.
Logging must never break an admin request: every failure is swallowed and
reported through the application logger instead.
"""

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import settings

logger = logging.getLogger("agent-rag.audit")

_lock = threading.Lock()


def _path() -> Path:
    return Path(settings.AUDIT_LOG_PATH)


def actor_label(token_present: bool, client_host: str) -> str:
    """Identify who performed the action without ever storing the secret."""
    if token_present:
        digest = hashlib.sha1(settings.ADMIN_TOKEN.encode()).hexdigest()[:8]
        return f"token:{digest}"
    return f"loopback:{client_host or 'unknown'}"


def log_event(
    action: str,
    actor: str,
    *,
    dept: Optional[str] = None,
    source: Optional[str] = None,
    ok: bool = True,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one audit record. Never raises."""
    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "actor": actor,
        "ok": ok,
    }
    if dept:
        record["dept"] = dept
    if source:
        record["source"] = source
    if detail:
        record["detail"] = detail
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        logger.warning("could not write audit record: %s", record, exc_info=True)


def tail(limit: int = 50) -> list:
    """Return the most recent audit records (newest last)."""
    path = _path()
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
    except Exception:
        logger.warning("could not read audit log", exc_info=True)
        return []
