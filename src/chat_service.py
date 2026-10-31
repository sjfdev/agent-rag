"""Chat orchestration shared by `/chat` and `/chat/stream`.

The two endpoints used to duplicate validation, maintenance checks, cache
lookups, error classification and metric recording. Keeping that in one place
means a behaviour change only has to be made once.

This module is framework-light on purpose: the only FastAPI dependency is
`HTTPException`, which callers translate into their own response type. Streaming
yields already-encoded SSE frames.
"""

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage

from src import metrics
from src.agent import app as agent_app
from src.cache import answer_cache
from src.config import settings
from src.errors import ChatTimeout, classify_error, is_store_error, is_upstream_error
from src.ratelimit import RateLimiter
from src.runtime import in_maintenance
from src.schemas import ChatRequest, ChatResponse
from src.validation import InvalidDepartment, validate_depts

logger = logging.getLogger("agent-rag.chat")

__all__ = [
    "ChatFailure",
    "ChatTimeout",
    "Prepared",
    "cache_bypassed",
    "cache_lookup",
    "cache_store",
    "classify_error",
    "is_store_error",
    "is_upstream_error",
    "open_stream",
    "prepare",
    "ratelimiter",
    "run",
]

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chat-deadline")


# Per-caller limit; recreated if the configured value changes (tests).
_limiter_lock = threading.Lock()
_limiter: Optional[RateLimiter] = None
_limiter_config = None


def ratelimiter() -> RateLimiter:
    global _limiter, _limiter_config
    config = (settings.RATE_LIMIT_PER_MINUTE, 60.0)
    with _limiter_lock:
        if _limiter is None or _limiter_config != config:
            _limiter = RateLimiter(limit=config[0], window=config[1])
            _limiter_config = config
        return _limiter


class ChatFailure(Exception):
    """A classified chat failure carrying the HTTP mapping and request id."""

    def __init__(self, request_id: str, exc: Exception):
        self.request_id = request_id
        self.status_code, self.code, self.message = classify_error(exc)
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# request preparation
# ---------------------------------------------------------------------------

def trim_history(history: List[Any], limit: Optional[int] = None) -> List[Any]:
    """Keep the most recent messages, starting at a user turn.

    Oldest messages are dropped first. If the cut lands on an assistant
    message, it is dropped too so the model never sees a reply without its
    question (some providers reject that, and it confuses follow-up rewriting).
    """
    if not history:
        return []
    budget = settings.HISTORY_MAX_MESSAGES if limit is None else limit
    if budget <= 0:
        return []
    kept = list(history[-budget:])
    while kept and kept[0].role != "user":
        kept.pop(0)
    return kept


@dataclass
class Prepared:
    request_id: str
    endpoint: str
    started: float
    inputs: Dict[str, Any]


def _client_key(request: ChatRequest, headers: Any) -> str:
    """Rate-limit identity: real client IP when known, else the user id.

    Behind a proxy the forwarded address is the only value an abusive caller
    cannot rotate cheaply, so prefer it. Without one (direct or tests) the user
    id keeps limits per caller instead of lumping everyone together.
    """
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    forwarded = lowered.get("x-forwarded-for", "").split(",")[0].strip()
    client_ip = forwarded or lowered.get("x-real-ip", "").strip()
    if client_ip:
        return f"ip:{client_ip}"
    return f"user:{request.user_id or 'unknown'}"


def prepare(
    request: ChatRequest, endpoint: str, headers: Any = None
) -> Prepared:
    """Validate the payload and build graph inputs.

    Raises HTTPException for maintenance (503), rate limiting (429) and invalid
    departments (400), recording the rejection metric exactly once.
    """
    request_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()

    allowed, retry_after = ratelimiter().allow(_client_key(request, headers))
    if not allowed:
        metrics.observe_request(
            endpoint=endpoint,
            status="rejected",
            elapsed_seconds=time.perf_counter() - started,
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "message": "too many requests",
                "request_id": request_id,
            },
            headers={
                "X-Request-ID": request_id,
                "Retry-After": str(int(max(retry_after, 1))),
            },
        )

    if in_maintenance():
        metrics.observe_request(
            endpoint=endpoint,
            status="rejected",
            elapsed_seconds=time.perf_counter() - started,
        )
        raise HTTPException(
            status_code=503,
            detail={"error": "index rebuilding", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    try:
        safe_depts = validate_depts(request.departments())
    except InvalidDepartment:
        logger.warning(
            "rejected invalid dept request_id=%s depts=%r",
            request_id,
            request.departments(),
        )
        metrics.observe_request(
            endpoint=endpoint,
            status="rejected",
            elapsed_seconds=time.perf_counter() - started,
        )
        raise HTTPException(
            status_code=400,
            detail="invalid department name",
            headers={"X-Request-ID": request_id},
        )

    messages = []
    for item in trim_history(request.history):
        cls = HumanMessage if item.role == "user" else AIMessage
        messages.append(cls(content=item.content))
    messages.append(HumanMessage(content=request.message))

    return Prepared(
        request_id=request_id,
        endpoint=endpoint,
        started=started,
        inputs={
            "messages": messages,
            "user_id": request.user_id,
            # Primary department keeps single-dept behaviour and citations.
            "user_dept": safe_depts[0],
            "user_depts": safe_depts,
            "user_groups": list(request.user_groups or []),
        },
    )


# ---------------------------------------------------------------------------
# timings / SSE helpers
# ---------------------------------------------------------------------------

def timings_from(final_state: Dict[str, Any], started: float) -> Dict[str, Any]:
    timings = {
        k: (int(v) if k.endswith("_calls") else round(float(v), 1))
        for k, v in (final_state.get("timings") or {}).items()
    }
    timings.setdefault("rerank_ms", 0.0)
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    timings["overhead_ms"] = round(
        timings["total_ms"]
        - timings.get("retrieval_ms", 0.0)
        - timings.get("llm_ms", 0.0)
        - timings.get("rerank_ms", 0.0),
        1,
    )
    return timings


def cached_timings(started: float) -> Dict[str, Any]:
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    return {
        "retrieval_ms": 0.0,
        "rerank_ms": 0.0,
        "llm_ms": 0.0,
        "llm_calls": 0,
        "total_ms": elapsed,
        "overhead_ms": elapsed,
        "cached": True,
    }


def sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# answer cache
# ---------------------------------------------------------------------------

def cache_bypassed(headers: Any) -> bool:
    """Clients can force a fresh answer with `Cache-Control: no-cache`."""
    lowered = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    if lowered.get("x-no-cache", "") in {"1", "true", "yes"}:
        return True
    return "no-cache" in lowered.get("cache-control", "")


def cache_lookup(
    request: ChatRequest, headers: Any
) -> Optional[Dict[str, Any]]:
    """Only single-turn questions are cached: follow-ups depend on history."""
    if not _cache_enabled() or request.history or cache_bypassed(headers):
        return None
    payload = answer_cache.get(_cache_dept(request), request.message)
    metrics.observe_cache("answer", "hit" if payload is not None else "miss")
    return payload


def cache_store(
    request: ChatRequest,
    headers: Any,
    final_state: Dict[str, Any],
    timings: Dict[str, Any],
) -> None:
    """Cache only replay-safe answers (one LLM call, no tool use)."""
    if not _cache_enabled() or request.history or cache_bypassed(headers):
        return
    if int(timings.get("llm_calls") or 0) != 1:
        return
    if timings.get("degraded"):
        return
    messages = final_state.get("messages") or []
    if not messages:
        return
    answer_cache.put(
        _cache_dept(request),
        request.message,
        {
            "response": messages[-1].content,
            "sources": final_state.get("sources") or [],
        },
    )
    metrics.observe_cache("answer", "store")


def _cache_enabled() -> bool:
    return settings.FAQ_CACHE_ENABLED and answer_cache.max_entries > 0


def _cache_dept(request: ChatRequest) -> str:
    """Cache key scope: departments plus ACL groups.

    Groups must be part of the key, otherwise an answer built with elevated
    access could be replayed for a caller without it.
    """
    depts = request.departments()
    scope = "|".join(depts) if depts else "unknown"
    groups = sorted(str(group) for group in (request.user_groups or []))
    if settings.ACL_ENABLED and groups:
        scope += "::" + ",".join(groups)
    return scope


# ---------------------------------------------------------------------------
# non-streaming
# ---------------------------------------------------------------------------

def run(request: ChatRequest, headers: Any, endpoint: str = "chat") -> ChatResponse:
    prepared = prepare(request, endpoint, headers)
    started = prepared.started

    cached = cache_lookup(request, headers)
    if cached is not None:
        timings = cached_timings(started)
        metrics.observe_request(
            endpoint=endpoint,
            status="cached",
            elapsed_seconds=time.perf_counter() - started,
            timings=timings,
        )
        return ChatResponse(
            response=cached.get("response", ""),
            sources=cached.get("sources") or [],
            timings=timings,
            request_id=prepared.request_id,
        )

    try:
        final_state = _invoke_with_deadline(prepared.inputs)
    except (FutureTimeout, ChatTimeout) as exc:
        _record_failure(prepared, exc)
        raise ChatFailure(prepared.request_id, exc) from exc
    except Exception as exc:
        _record_failure(prepared, exc)
        raise ChatFailure(prepared.request_id, exc) from exc

    timings = timings_from(final_state, started)
    degraded = bool(final_state.get("degraded"))
    if degraded:
        timings["degraded"] = True
    else:
        cache_store(request, headers, final_state, timings)
    logger.info(
        "chat ok request_id=%s dept=%s total_ms=%s llm_ms=%s llm_calls=%s",
        prepared.request_id,
        ",".join(prepared.inputs.get("user_depts") or []) or "?",
        timings["total_ms"],
        timings.get("llm_ms"),
        timings.get("llm_calls"),
    )
    metrics.observe_request(
        endpoint=endpoint,
        status="ok",
        elapsed_seconds=time.perf_counter() - started,
        timings=timings,
    )
    return ChatResponse(
        response=final_state["messages"][-1].content,
        sources=final_state.get("sources") or [],
        timings=timings,
        request_id=prepared.request_id,
        degraded=degraded,
        degraded_reason=(final_state.get("degraded_reason") or "") if degraded else "",
    )


def _invoke_with_deadline(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Run the graph with an overall deadline.

    The worker thread cannot be killed, but the caller stops waiting and the
    request fails fast instead of hanging for minutes.
    """
    budget = max(int(settings.CHAT_TIMEOUT or 0), 0)
    if budget <= 0:
        return agent_app.invoke(
            inputs, config={"recursion_limit": settings.AGENT_RECURSION_LIMIT}
        )
    future = _executor.submit(
        agent_app.invoke,
        inputs,
        config={"recursion_limit": settings.AGENT_RECURSION_LIMIT},
    )
    try:
        return future.result(timeout=budget)
    except FutureTimeout:
        future.cancel()
        raise ChatTimeout(f"agent exceeded {budget}s")


def _record_failure(prepared: Prepared, exc: Exception) -> None:
    logger.exception(
        "chat failed request_id=%s dept=%s upstream=%s store_down=%s",
        prepared.request_id,
        ",".join(prepared.inputs.get("user_depts") or []) or "?",
        is_upstream_error(exc),
        is_store_error(exc),
    )
    metrics.observe_request(
        endpoint=prepared.endpoint,
        status="error",
        elapsed_seconds=time.perf_counter() - prepared.started,
    )


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------

def open_stream(
    request: ChatRequest, headers: Any, endpoint: str = "chat_stream"
) -> tuple:
    """Validate now and return (request_id, frame iterator).

    Preparing eagerly means validation and maintenance faults surface as HTTP
    errors before a 200 stream is opened.
    """
    prepared = prepare(request, endpoint, headers)
    return prepared.request_id, _stream(prepared, request, headers)


def _stream(
    prepared: Prepared, request: ChatRequest, headers: Any
) -> Iterator[str]:
    """Yield SSE frames: tokens, then a done event (or an error event)."""
    started = prepared.started
    request_id = prepared.request_id
    final_state: Dict[str, Any] = {}
    first_token_at = None

    cached = cache_lookup(request, headers)
    if cached is not None:
        timings = cached_timings(started)
        metrics.observe_request(
            endpoint=prepared.endpoint,
            status="cached",
            elapsed_seconds=time.perf_counter() - started,
            timings=timings,
        )
        yield sse({"type": "token", "content": cached.get("response", "")})
        yield sse(
            {
                "type": "done",
                "sources": cached.get("sources") or [],
                "timings": timings,
            }
        )
        return

    try:
        for mode, payload in agent_app.stream(
            prepared.inputs,
            stream_mode=["messages", "values"],
            config={"recursion_limit": settings.AGENT_RECURSION_LIMIT},
        ):
            if mode == "messages":
                chunk, meta = payload
                if meta.get("langgraph_node") != "agent":
                    continue
                if settings.CHAT_TIMEOUT and (
                    time.perf_counter() - started > settings.CHAT_TIMEOUT
                ):
                    raise ChatTimeout(f"agent exceeded {settings.CHAT_TIMEOUT}s")
                text = getattr(chunk, "content", "")
                if text:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                        metrics.observe_first_token(
                            prepared.endpoint, first_token_at - started
                        )
                    yield sse({"type": "token", "content": text})
            elif mode == "values":
                final_state = payload

        timings = timings_from(final_state, started)
        degraded = bool(final_state.get("degraded"))
        if degraded:
            timings["degraded"] = True
        else:
            cache_store(request, headers, final_state, timings)
        logger.info(
            "chat stream ok request_id=%s dept=%s total_ms=%s llm_ms=%s llm_calls=%s",
            request_id,
            ",".join(prepared.inputs.get("user_depts") or []) or "?",
            timings["total_ms"],
            timings.get("llm_ms"),
            timings.get("llm_calls"),
        )
        metrics.observe_request(
            endpoint=prepared.endpoint,
            status="ok",
            elapsed_seconds=time.perf_counter() - started,
            timings=timings,
        )
        done: Dict[str, Any] = {
            "type": "done",
            "sources": final_state.get("sources") or [],
            "timings": timings,
        }
        if degraded:
            done["degraded"] = True
            done["degraded_reason"] = final_state.get("degraded_reason") or ""
        yield sse(done)
    except Exception as exc:
        _record_failure(prepared, exc)
        _, code, message = classify_error(exc)
        yield sse(
            {
                "type": "error",
                "code": code,
                "message": message,
                "request_id": request_id,
            }
        )
