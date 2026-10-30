"""Prometheus metrics for the chat API.

Metrics are process-local (like the rest of the app state) and are exposed on
`GET /metrics`. Paths and request bodies are never used as labels to avoid
unbounded cardinality; only low-cardinality values do.
"""

import time
from typing import Any, Dict, Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)

_REQUESTS = Counter(
    "agent_rag_chat_requests_total",
    "Chat requests by endpoint and outcome.",
    ["endpoint", "status"],
    registry=REGISTRY,
)
_DURATION = Histogram(
    "agent_rag_chat_duration_seconds",
    "End-to-end chat latency.",
    ["endpoint", "status"],
    buckets=(0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0),
    registry=REGISTRY,
)
_RETRIEVAL = Histogram(
    "agent_rag_retrieval_seconds",
    "Vector retrieval latency (including optional reranking).",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)
_LLM = Histogram(
    "agent_rag_llm_seconds",
    "Time spent waiting on the LLM.",
    ["calls"],
    buckets=(0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0),
    registry=REGISTRY,
)
_FIRST_TOKEN = Histogram(
    "agent_rag_first_token_seconds",
    "Time to first streamed token.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0),
    registry=REGISTRY,
)
_RETRIEVAL_ENGINE = Counter(
    "agent_rag_retrieval_engine_total",
    "Retrieval calls by engine (vector / local / milvus / fallback).",
    ["engine"],
    registry=REGISTRY,
)
_LLM_CALLS = Counter(
    "agent_rag_llm_calls_total",
    "LLM completion calls (multiple per request when a tool is used).",
    ["endpoint"],
    registry=REGISTRY,
)
_INDEX_CHUNKS = Gauge(
    "agent_rag_index_chunks",
    "Number of chunks currently indexed (refreshed on scrape).",
    registry=REGISTRY,
)
_INGEST_JOBS = Counter(
    "agent_rag_ingest_jobs_total",
    "Ingestion jobs by final status.",
    ["status"],
    registry=REGISTRY,
)
_MAINTENANCE = Gauge(
    "agent_rag_maintenance",
    "1 while a full index rebuild is in progress.",
    registry=REGISTRY,
)
_HYBRID_ENGINE = Gauge(
    "agent_rag_hybrid_engine",
    "Active hybrid retrieval engine (1 for the selected engine).",
    ["engine"],
    registry=REGISTRY,
)
_HYBRID_SPARSE_READY = Gauge(
    "agent_rag_hybrid_sparse_ready",
    "1 when the vector store can serve server-side BM25 sparse search.",
    registry=REGISTRY,
)


def set_hybrid(engine: str, sparse_ready: bool) -> None:
    for name in ("off", "local", "milvus"):
        _HYBRID_ENGINE.labels(engine=name).set(1.0 if name == engine else 0.0)
    _HYBRID_SPARSE_READY.set(1.0 if sparse_ready else 0.0)

_INDEX_REFRESH_SECONDS = 5.0
_index_state = {"at": 0.0, "refreshing": False, "ok": 1.0}


def _refresh_index_gauge() -> None:
    """Read the chunk count at most every few seconds, and never raise."""
    now = time.monotonic()
    if now - _index_state["at"] < _INDEX_REFRESH_SECONDS or _index_state["refreshing"]:
        return
    _index_state["refreshing"] = True
    try:
        from src.engine import kb_engine

        _INDEX_CHUNKS.set(kb_engine.live_count())
        _index_state["ok"] = 1.0
    except Exception:
        _index_state["ok"] = 0.0
    finally:
        _index_state["at"] = time.monotonic()
        _index_state["refreshing"] = False


def observe_request(
    endpoint: str,
    status: str,
    elapsed_seconds: float,
    timings: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one finished chat request.

    Department names are deliberately not used as labels: they are sensitive
    and unbounded, and /metrics is scraped by infrastructure, not end users.
    """
    _REQUESTS.labels(endpoint=endpoint, status=status).inc()
    _DURATION.labels(endpoint=endpoint, status=status).observe(elapsed_seconds)

    timings = timings or {}
    retrieval_ms = timings.get("retrieval_ms")
    if isinstance(retrieval_ms, (int, float)) and retrieval_ms > 0:
        _RETRIEVAL.observe(float(retrieval_ms) / 1000)
    llm_ms = timings.get("llm_ms")
    calls = timings.get("llm_calls") or 0
    if isinstance(llm_ms, (int, float)) and llm_ms > 0:
        _LLM.labels(calls=str(int(calls))).observe(float(llm_ms) / 1000)
    if calls:
        _LLM_CALLS.labels(endpoint=endpoint).inc(int(calls))


def observe_retrieval(engine: str) -> None:
    _RETRIEVAL_ENGINE.labels(engine=engine).inc()


def observe_first_token(endpoint: str, seconds: float) -> None:
    _FIRST_TOKEN.observe(seconds)


def observe_ingest_job(status: str) -> None:
    _INGEST_JOBS.labels(status=status).inc()


def set_maintenance(active: bool) -> None:
    _MAINTENANCE.set(1 if active else 0)


def render() -> bytes:
    """Refresh gauges that depend on live state, then return the exposition text."""
    from src.engine import kb_engine
    from src.runtime import in_maintenance

    _MAINTENANCE.set(1 if in_maintenance() else 0)
    try:
        status = kb_engine.hybrid_status()
        set_hybrid(status["engine"], status["sparse_ready"])
    except Exception:
        pass
    _refresh_index_gauge()
    return generate_latest(REGISTRY)
