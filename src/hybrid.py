"""Capability-adaptive hybrid retrieval helpers.

The engine tries to use Milvus server-side sparse BM25 when the server is
2.5+ and the collection actually has a sparse field with a BM25 index.
Otherwise it falls back to the in-process BM25 index. These helpers keep the
decision logic pure so it can be unit-tested without a Milvus instance.
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple

from src.config import settings

logger = logging.getLogger(__name__)

VERSION_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")


def connection_args() -> Dict[str, Any]:
    """Milvus connection kwargs from settings."""
    if settings.MILVUS_URI:
        return {"uri": settings.MILVUS_URI}
    return {"host": settings.MILVUS_HOST, "port": settings.MILVUS_PORT}


def parse_version(raw: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """Parse ``v2.5.4`` / ``2.5.0`` / ``2.5`` into a comparable tuple."""
    if not raw:
        return None
    match = VERSION_RE.search(str(raw))
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def version_at_least(raw: Optional[str], minimum: str) -> bool:
    parsed = parse_version(raw)
    wanted = parse_version(minimum)
    if parsed is None or wanted is None:
        return False
    return parsed >= wanted


def resolve_hybrid_mode(
    configured: str,
    enabled: bool,
    sparse_supported: bool,
) -> str:
    """Return ``off`` / ``local`` / ``milvus``.

    ``auto`` prefers the server-side sparse engine when it is genuinely usable
    and falls back to the in-process BM25 index otherwise. Explicit ``milvus``
    is honoured even when unsupported, so validation can fail loudly instead of
    silently degrading.
    """
    if not enabled:
        return "off"
    choice = (configured or "auto").lower()
    if choice == "local":
        return "local"
    if choice == "milvus":
        return "milvus"
    if choice != "auto":
        raise ValueError(
            f"HYBRID_ENGINE must be auto|local|milvus, got {configured!r}"
        )
    return "milvus" if sparse_supported else "local"


def explicit_milvus(configured: str, enabled: bool) -> bool:
    return enabled and (configured or "").lower() == "milvus"
