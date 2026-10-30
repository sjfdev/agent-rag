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
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage

from src import metrics
from src.agent import app as agent_app
from src.cache import answer_cache
from src.config import settings
from src.runtime import in_maintenance
from src.schemas import ChatRequest, ChatResponse
from src.validation import InvalidDepartment, validate_dept

logger = logging.getLogger("agent-rag.chat")

# Vector-store connection failures (Milvus down, FAISS index unreadable).
try:
    from pymilvus.exceptions import MilvusException
except Exception:  # pragma: no cover
    MilvusException = ()

try:  # openai is a hard dependency of langchain-openai
    from openai import APIConnectionError, APITimeoutError
except Exception:  # pragma: no cover
    APIConnectionError = APITimeoutError = ()


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------

def is_upstream_error(exc: Exception) -> bool:
    """Network/DNS/timeout failures while talking to the LLM provider."""
    if APIConnectionError and isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(
        names & {"ConnectError", "ReadTimeout", "ConnectTimeout", "APITimeoutError"}
    )


def is_store_error(exc: Exception) -> bool:
    """The vector store is unreachable (as opposed to a bad request or a bug)."""
    if MilvusException and isinstance(exc, MilvusException):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & {"ConnectionError", "ConnectionRefusedError", "MilvusException"})


def classify_error(exc: Exception) -> tuple:
    """Map an exception to (http_status, code, message)."""
    if is_store_error(exc):
        return 503, "vector_store_unavailable", "vector store unavailable"
    if is_upstream_error(exc):
        return 503, "upstream_unavailable", "llm upstream unavailable"
    return 500, "internal_error", "internal error"


class ChatFailure(Exception):
    """A classified chat failure carrying the HTTP mapping and request id."""

    def __init__(self, request_id: str, exc: Exception):
        self.request_id = request_id
        self.status_code, self.code, self.message = classify_error(exc)
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# request preparation
# ---------------------------------------------------------------------------

@dataclass
class Prepared:
    request_id: str
    endpoint: str
    started: float
    inputs: Dict[str, Any]


def prepare(request: ChatRequest, endpoint: str) -> Prepared:
    """Validate the payload and build graph inputs.

    Raises HTTPException for maintenance (503) and invalid departments (400),
    recording the rejection metric exactly once.
    """
    request_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()

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
        safe_dept = validate_dept(request.user_dept)
    except InvalidDepartment:
        logger.warning(
            "rejected invalid dept request_id=%s dept=%r", request_id, request.user_dept
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
    for item in request.history:
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
            "user_dept": safe_dept,
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
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    timings["overhead_ms"] = round(
        timings["total_ms"]
        - timings.get("retrieval_ms", 0.0)
        - timings.get("llm_ms", 0.0),
        1,
    )
    return timings


def cached_timings(started: float) -> Dict[str, Any]:
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    return {
        "retrieval_ms": 0.0,
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
    payload = answer_cache.get(request.user_dept, request.message)
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
    messages = final_state.get("messages") or []
    if not messages:
        return
    answer_cache.put(
        request.user_dept,
        request.message,
        {
            "response": messages[-1].content,
            "sources": final_state.get("sources") or [],
        },
    )
    metrics.observe_cache("answer", "store")


def _cache_enabled() -> bool:
    return settings.FAQ_CACHE_ENABLED and answer_cache.max_entries > 0


# ---------------------------------------------------------------------------
# non-streaming
# ---------------------------------------------------------------------------

def run(request: ChatRequest, headers: Any, endpoint: str = "chat") -> ChatResponse:
    prepared = prepare(request, endpoint)
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
        final_state = agent_app.invoke(
            prepared.inputs,
            config={"recursion_limit": settings.AGENT_RECURSION_LIMIT},
        )
    except Exception as exc:
        _record_failure(prepared, exc)
        raise ChatFailure(prepared.request_id, exc) from exc

    timings = timings_from(final_state, started)
    cache_store(request, headers, final_state, timings)
    logger.info(
        "chat ok request_id=%s dept=%s total_ms=%s llm_ms=%s llm_calls=%s",
        prepared.request_id,
        prepared.inputs["user_dept"],
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
    )


def _record_failure(prepared: Prepared, exc: Exception) -> None:
    logger.exception(
        "chat failed request_id=%s dept=%s upstream=%s store_down=%s",
        prepared.request_id,
        prepared.inputs["user_dept"],
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
    prepared = prepare(request, endpoint)
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
        cache_store(request, headers, final_state, timings)
        logger.info(
            "chat stream ok request_id=%s dept=%s total_ms=%s llm_ms=%s llm_calls=%s",
            request_id,
            prepared.inputs["user_dept"],
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
        yield sse(
            {
                "type": "done",
                "sources": final_state.get("sources") or [],
                "timings": timings,
            }
        )
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
