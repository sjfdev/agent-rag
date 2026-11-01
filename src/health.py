"""Health and readiness endpoints.

`/health` is cheap enough for load balancers: it only inspects the vector
store and the newest ingestion job. Add `?probe=1` to also call the LLM
completion endpoint; successful probes are cached for a few seconds so a
frequent monitor does not spend tokens on every request.
"""

import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from src.admin_api import job_snapshot
from src.config import settings
from src.engine import kb_engine
from src.runtime import in_maintenance

router = APIRouter()

_PROBE_TTL = 15.0
_probe_lock = threading.Lock()
_probe_cache: Dict[str, Any] = {"at": 0.0, "result": None}


def _probe_llm() -> Dict[str, Any]:
    """Call /chat/completions with a one-token ping; cached for _PROBE_TTL."""
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache["result"]
        if cached is not None and now - _probe_cache["at"] < _PROBE_TTL:
            return dict(cached, cached=True)

    result = {"status": "ok", "latency_ms": None, "error": None}
    started = time.perf_counter()
    try:
        import requests

        base = settings.LLM_API_BASE.rstrip("/")
        response = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
            json={
                "model": settings.LLM_MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "temperature": 0,
            },
            timeout=min(settings.LLM_TIMEOUT, 5),
        )
        if response.status_code >= 400:
            result.update(status="failed", error=f"HTTP {response.status_code}")
    except Exception as exc:
        result.update(status="failed", error=type(exc).__name__)
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)

    with _probe_lock:
        _probe_cache["at"] = time.monotonic()
        _probe_cache["result"] = dict(result)
    return result


_DISK_PATHS = (
    ("data", "DATA_DIR"),
    ("index", "DB_PATH"),
    ("jobs", "JOBS_PATH"),
    ("audit", "AUDIT_LOG_PATH"),
)


def _free_mb(path: str) -> float | None:
    """Free space for `path`, falling back to its nearest existing ancestor."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return round(shutil.disk_usage(probe).free / (1024 * 1024), 1)
    except OSError:
        return None


def storage_report() -> Dict[str, Any]:
    """Flag the directories this process writes to when they run low.

    A full disk does not fail queries, so /health used to stay green while
    ingestion silently broke. Milvus is worse: the first ENOSPC is cached in
    RocksDB, so writes keep failing after space is freed until the server is
    restarted. Reporting the tightest directory makes that state visible.
    """
    limiting = None
    paths: list[Dict[str, Any]] = []
    for label, attr in _DISK_PATHS:
        free = _free_mb(getattr(settings, attr))
        paths.append({"name": label, "path": getattr(settings, attr), "free_mb": free})
        if free is not None and (limiting is None or free < limiting["free_mb"]):
            limiting = paths[-1]

    min_free = settings.HEALTH_MIN_FREE_MB
    ok = limiting is None or min_free <= 0 or limiting["free_mb"] >= min_free
    return {
        "ok": ok,
        "min_free_mb": min_free,
        "free_mb": None if limiting is None else limiting["free_mb"],
        "path": None if limiting is None else limiting["path"],
        "paths": paths,
    }


@router.get("/health")
def health(probe: bool = Query(False, description="also call the LLM endpoint")):
    checks: Dict[str, Any] = {}
    degraded = False
    down = False

    maintenance = in_maintenance()
    if maintenance:
        degraded = True

    store: Dict[str, Any] = {
        "type": settings.VECTOR_STORE_TYPE,
        "ok": True,
        "chunks": None,
        "error": None,
        "latency_ms": None,
    }
    started = time.perf_counter()
    try:
        # Always report the live index (what queries actually see), even while
        # a rebuild is writing into a shadow collection.
        store["chunks"] = kb_engine.live_count()
    except Exception as exc:
        store.update(ok=False, error=type(exc).__name__)
        down = True
    store["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)

    min_chunks = settings.HEALTH_MIN_CHUNKS
    if store["ok"] and store["chunks"] is not None:
        if store["chunks"] < min_chunks:
            down = True
        elif store["chunks"] == 0:
            degraded = True
    checks["vector_store"] = store

    latest = job_snapshot()
    active = bool(latest and latest.get("status") in {"queued", "running"})
    if active:
        degraded = True
    checks["ingestion"] = {"active": active, "latest": latest}

    llm: Dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "probe": "skipped",
        "latency_ms": None,
        "error": None,
    }
    if probe:
        result = _probe_llm()
        llm.update(
            probe=result["status"],
            latency_ms=result["latency_ms"],
            error=result["error"],
        )
        if result["status"] != "ok":
            degraded = True
    checks["llm"] = llm

    storage = storage_report()
    checks["storage"] = storage
    if not storage["ok"]:
        degraded = True

    from src import acl

    acl_state = acl.status()
    checks["acl"] = acl_state
    if acl_state["enabled"] and not acl_state["ok"]:
        # Documents would be filtered out (or the app refused to start); make
        # the readiness probe say so instead of looking healthy.
        down = True

    status = "down" if down else ("degraded" if degraded else "ok")
    checks["status"] = status
    checks["maintenance"] = maintenance
    checks["time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    checks["rerank_enabled"] = settings.RERANK_ENABLED
    from src.cache import answer_cache

    checks["cache"] = {
        "enabled": settings.FAQ_CACHE_ENABLED,
        "ttl_seconds": settings.FAQ_CACHE_TTL,
        "answer": answer_cache.stats(),
        "embeddings": kb_engine.cache_status().get("embeddings"),
    }
    checks["hybrid_enabled"] = settings.HYBRID_ENABLED
    hybrid = kb_engine.hybrid_status()
    checks["hybrid"] = hybrid
    if hybrid["engine"] == "local" and hybrid["configured"] == "auto":
        # Not a failure: auto mode intentionally falls back when the server or
        # the collection cannot do sparse BM25.
        pass
    elif hybrid["enabled"] and hybrid["configured"] == "milvus" and not hybrid["sparse_ready"]:
        degraded = True

    code = 503 if status == "down" else 200
    return JSONResponse(status_code=code, content=checks)
