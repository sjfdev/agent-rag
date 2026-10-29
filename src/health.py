"""Health and readiness endpoints.

`/health` is cheap enough for load balancers: it only inspects the vector
store and the newest ingestion job. Add `?probe=1` to also call the LLM
completion endpoint; successful probes are cached for a few seconds so a
frequent monitor does not spend tokens on every request.
"""

import threading
import time
from datetime import datetime, timezone
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
        store["chunks"] = kb_engine.count()
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

    status = "down" if down else ("degraded" if degraded else "ok")
    checks["status"] = status
    checks["maintenance"] = maintenance
    checks["time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    checks["rerank_enabled"] = settings.RERANK_ENABLED
    checks["hybrid_enabled"] = settings.HYBRID_ENABLED

    code = 503 if status == "down" else 200
    return JSONResponse(status_code=code, content=checks)
