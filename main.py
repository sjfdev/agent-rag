import json
import logging
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from src.admin_api import router as admin_router
from src.agent import app as agent_app
from src.cache import answer_cache
from src.config import settings
from src.runtime import in_maintenance
from src.engine import kb_engine
from src.health import router as health_router
from src import metrics
from src.validation import InvalidDepartment, validate_dept

# Vector-store connection failures (Milvus down, FAISS index unreadable).
try:
    from pymilvus.exceptions import MilvusException
except Exception:  # pragma: no cover
    MilvusException = ()

try:  # openai is a hard dependency of langchain-openai
    from openai import APIConnectionError, APITimeoutError
except Exception:  # pragma: no cover
    APIConnectionError = APITimeoutError = ()


def _is_upstream_error(exc: Exception) -> bool:
    """Network/DNS/timeout failures while talking to the LLM provider."""
    if APIConnectionError and isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & {"ConnectError", "ReadTimeout", "ConnectTimeout", "APITimeoutError"})


def _is_store_error(exc: Exception) -> bool:
    """The vector store is unreachable (as opposed to a bad request or a bug)."""
    if MilvusException and isinstance(exc, MilvusException):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & {"ConnectionError", "ConnectionRefusedError", "MilvusException"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent-rag")

app = FastAPI(title="Enterprise KB Agent API")

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui/")


app.include_router(admin_router)
app.include_router(health_router)


@app.get("/admin", include_in_schema=False)
def admin_page():
    page = STATIC_DIR / "admin.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="admin UI not found")
    return FileResponse(page)


@app.get("/info")
def info():
    return {
        "vector_store": settings.VECTOR_STORE_TYPE,
        "llm_model": settings.LLM_MODEL,
        "embedding_backend": settings.EMBEDDING_BACKEND,
        "hybrid": kb_engine.hybrid_status(),
    }


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    user_dept: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    history: List[HistoryItem] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    response: str
    sources: List[Dict[str, Any]] = []
    timings: Dict[str, Any] = {}


def _prepare(request: ChatRequest) -> Dict[str, Any]:
    """Validate the department and convert the payload into graph input."""
    safe_dept = validate_dept(request.user_dept)  # raises InvalidDepartment

    messages = []
    for item in request.history:
        cls = HumanMessage if item.role == "user" else AIMessage
        messages.append(cls(content=item.content))
    messages.append(HumanMessage(content=request.message))

    return {
        "messages": messages,
        "user_id": request.user_id,
        "user_dept": safe_dept,
    }


def _timings(final_state: Dict[str, Any], started: float) -> Dict[str, Any]:
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


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _cache_enabled() -> bool:
    return settings.FAQ_CACHE_ENABLED and answer_cache.max_entries > 0


def _cache_bypassed(http_request: Request) -> bool:
    """Clients can force a fresh answer with `Cache-Control: no-cache`."""
    if http_request.headers.get("x-no-cache", "").lower() in {"1", "true", "yes"}:
        return True
    return "no-cache" in http_request.headers.get("cache-control", "").lower()


def _cache_lookup(request: ChatRequest, http_request: Request) -> Optional[Dict[str, Any]]:
    """Only single-turn questions are cached: follow-ups depend on history."""
    if not _cache_enabled() or request.history or _cache_bypassed(http_request):
        return None
    payload = answer_cache.get(request.user_dept, request.message)
    if payload is None:
        metrics.observe_cache("answer", "miss")
        return None
    metrics.observe_cache("answer", "hit")
    return payload


def _cache_store(
    request: ChatRequest,
    http_request: Request,
    final_state: Dict[str, Any],
    timings: Dict[str, Any],
) -> None:
    """Cache only replay-safe answers (one LLM call, no tool use)."""
    if not _cache_enabled() or request.history or _cache_bypassed(http_request):
        return
    if int(timings.get("llm_calls") or 0) != 1:
        return
    answer = final_state.get("messages") or []
    if not answer:
        return
    payload = {
        "response": answer[-1].content,
        "sources": final_state.get("sources") or [],
    }
    answer_cache.put(request.user_dept, request.message, payload)
    metrics.observe_cache("answer", "store")


def _cached_timings(payload: Dict[str, Any], started: float) -> Dict[str, Any]:
    return {
        "retrieval_ms": 0.0,
        "llm_ms": 0.0,
        "llm_calls": 0,
        "total_ms": round((time.perf_counter() - started) * 1000, 1),
        "overhead_ms": round((time.perf_counter() - started) * 1000, 1),
        "cached": True,
    }


@app.on_event("startup")
async def startup_event():
    # Data is loaded by `python run_ingestion.py`; just warn if the store is empty.
    if kb_engine.is_empty():
        logger.warning(
            "vector store is empty; run `python run_ingestion.py` first "
            "(GET /health will report down)"
        )
        return
    if settings.HYBRID_ENABLED:
        # Build the BM25 indexes off the request path so the first search is fast.
        threading.Thread(
            target=_warm_lexical, name="lexical-warmup", daemon=True
        ).start()


def _warm_lexical() -> None:
    started = time.perf_counter()
    try:
        depts = kb_engine.warm_lexical()
        logger.info(
            "lexical warmup done depts=%s elapsed_ms=%.0f",
            ",".join(depts) or "-",
            (time.perf_counter() - started) * 1000,
        )
    except Exception:
        logger.warning("lexical warmup failed", exc_info=True)


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(request: Request):
    from fastapi.responses import Response as RawResponse

    if settings.METRICS_TOKEN:
        auth = request.headers.get("authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {settings.METRICS_TOKEN}"):
            raise HTTPException(status_code=401, detail="invalid metrics token")
    return RawResponse(
        content=metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, response: Response, http_request: Request):
    # Sync def: FastAPI runs it in a threadpool so LLM calls don't block the loop.
    request_id = uuid.uuid4().hex[:8]
    response.headers["X-Request-ID"] = request_id
    if in_maintenance():
        raise HTTPException(
            status_code=503,
            detail={"error": "index rebuilding", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )
    started = time.perf_counter()

    try:
        inputs = _prepare(request)
    except InvalidDepartment:
        logger.warning(
            "rejected invalid dept request_id=%s dept=%r", request_id, request.user_dept
        )
        metrics.observe_request(
            endpoint="chat",
            status="rejected",
            elapsed_seconds=time.perf_counter() - started,
        )
        raise HTTPException(status_code=400, detail="invalid department name")

    cached = _cache_lookup(request, http_request)
    if cached is not None:
        timings = _cached_timings(cached, started)
        metrics.observe_request(
            endpoint="chat",
            status="cached",
            elapsed_seconds=time.perf_counter() - started,
            timings=timings,
        )
        response.headers["X-Cache"] = "hit"
        return ChatResponse(
            response=cached.get("response", ""),
            sources=cached.get("sources") or [],
            timings=timings,
        )

    try:
        final_state = agent_app.invoke(
            inputs, config={"recursion_limit": settings.AGENT_RECURSION_LIMIT}
        )
        timings = _timings(final_state, started)
        _cache_store(request, http_request, final_state, timings)

        logger.info(
            "chat ok request_id=%s dept=%s total_ms=%s llm_ms=%s llm_calls=%s",
            request_id,
            inputs["user_dept"],
            timings["total_ms"],
            timings.get("llm_ms"),
            timings.get("llm_calls"),
        )
        metrics.observe_request(
            endpoint="chat",
            status="ok",
            elapsed_seconds=time.perf_counter() - started,
            timings=timings,
        )
        return ChatResponse(
            response=final_state["messages"][-1].content,
            sources=final_state.get("sources") or [],
            timings=timings,
        )
    except Exception as e:
        upstream = _is_upstream_error(e)
        store_down = _is_store_error(e)
        logger.exception(
            "chat failed request_id=%s dept=%s upstream=%s store_down=%s",
            request_id,
            inputs["user_dept"],
            upstream,
            store_down,
        )
        if store_down:
            code, message = "vector_store_unavailable", "vector store unavailable"
        elif upstream:
            code, message = "upstream_unavailable", "llm upstream unavailable"
        else:
            code, message = "internal_error", "internal error"
        metrics.observe_request(
            endpoint="chat",
            status="error",
            elapsed_seconds=time.perf_counter() - started,
        )
        raise HTTPException(
            status_code=503 if (upstream or store_down) else 500,
            detail={"error": code, "message": message, "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )


@app.post("/chat/stream")
def chat_stream(request: ChatRequest, http_request: Request):
    """Same as /chat but streams tokens as Server-Sent Events.

    Events: {"type":"token","content":...} ... {"type":"done","sources":[...],"timings":{...}}
    """
    request_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()

    if in_maintenance():
        raise HTTPException(
            status_code=503,
            detail={"error": "index rebuilding", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    try:
        inputs = _prepare(request)
    except InvalidDepartment:
        logger.warning(
            "rejected invalid dept request_id=%s dept=%r", request_id, request.user_dept
        )
        metrics.observe_request(
            endpoint="chat_stream",
            status="rejected",
            elapsed_seconds=time.perf_counter() - started,
        )
        raise HTTPException(status_code=400, detail="invalid department name")

    def event_stream():
        final_state: Dict[str, Any] = {}
        first_token_at = None
        cached = _cache_lookup(request, http_request)
        if cached is not None:
            timings = _cached_timings(cached, started)
            metrics.observe_request(
                endpoint="chat_stream",
                status="cached",
                elapsed_seconds=time.perf_counter() - started,
                timings=timings,
            )
            yield _sse({"type": "token", "content": cached.get("response", "")})
            yield _sse(
                {
                    "type": "done",
                    "sources": cached.get("sources") or [],
                    "timings": timings,
                }
            )
            return
        try:
            for mode, payload in agent_app.stream(
                inputs,
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
                                "chat_stream", first_token_at - started
                            )
                        yield _sse({"type": "token", "content": text})
                elif mode == "values":
                    final_state = payload

            timings = _timings(final_state, started)
            _cache_store(request, http_request, final_state, timings)
            logger.info(
                "chat stream ok request_id=%s dept=%s total_ms=%s llm_ms=%s llm_calls=%s",
                request_id,
                inputs["user_dept"],
                timings["total_ms"],
                timings.get("llm_ms"),
                timings.get("llm_calls"),
            )
            metrics.observe_request(
                endpoint="chat_stream",
                status="ok",
                elapsed_seconds=time.perf_counter() - started,
                timings=timings,
            )
            yield _sse(
                {
                    "type": "done",
                    "sources": final_state.get("sources") or [],
                    "timings": timings,
                }
            )
        except Exception as e:
            upstream = _is_upstream_error(e)
            store_down = _is_store_error(e)
            logger.exception(
                "chat stream failed request_id=%s dept=%s upstream=%s store_down=%s",
                request_id,
                inputs["user_dept"],
                upstream,
                store_down,
            )
            if store_down:
                code, message = "vector_store_unavailable", "vector store unavailable"
            elif upstream:
                code, message = "upstream_unavailable", "llm upstream unavailable"
            else:
                code, message = "internal_error", "internal error"
            metrics.observe_request(
                endpoint="chat_stream",
                status="error",
                elapsed_seconds=time.perf_counter() - started,
            )
            yield _sse(
                {
                    "type": "error",
                    "code": code,
                    "message": message,
                    "request_id": request_id,
                }
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "X-Request-ID": request_id,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
