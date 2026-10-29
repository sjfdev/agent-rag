import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from src.admin_api import router as admin_router
from src.agent import app as agent_app
from src.config import settings
from src.runtime import in_maintenance
from src.engine import kb_engine
from src.validation import InvalidDepartment, validate_dept

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


@app.on_event("startup")
async def startup_event():
    # Data is loaded by `python run_ingestion.py`; just warn if the store is empty.
    if kb_engine.is_empty():
        logger.warning("vector store is empty; run `python run_ingestion.py` first")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, response: Response):
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
        raise HTTPException(status_code=400, detail="invalid department name")

    try:
        final_state = agent_app.invoke(
            inputs, config={"recursion_limit": settings.AGENT_RECURSION_LIMIT}
        )
        timings = _timings(final_state, started)

        logger.info(
            "chat ok request_id=%s dept=%s total_ms=%s llm_ms=%s llm_calls=%s",
            request_id,
            inputs["user_dept"],
            timings["total_ms"],
            timings.get("llm_ms"),
            timings.get("llm_calls"),
        )
        return ChatResponse(
            response=final_state["messages"][-1].content,
            sources=final_state.get("sources") or [],
            timings=timings,
        )
    except Exception as e:
        upstream = _is_upstream_error(e)
        logger.exception(
            "chat failed request_id=%s dept=%s upstream=%s",
            request_id,
            inputs["user_dept"],
            upstream,
        )
        raise HTTPException(
            status_code=503 if upstream else 500,
            detail={
                "error": "llm upstream unavailable" if upstream else "internal error",
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )


@app.post("/chat/stream")
def chat_stream(request: ChatRequest):
    """Same as /chat but streams tokens as Server-Sent Events.

    Events: {"type":"token","content":...} ... {"type":"done","sources":[...],"timings":{...}}
    """
    request_id = uuid.uuid4().hex[:8]

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
        raise HTTPException(status_code=400, detail="invalid department name")

    def event_stream():
        started = time.perf_counter()
        final_state: Dict[str, Any] = {}
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
                        yield _sse({"type": "token", "content": text})
                elif mode == "values":
                    final_state = payload

            timings = _timings(final_state, started)
            logger.info(
                "chat stream ok request_id=%s dept=%s total_ms=%s llm_ms=%s llm_calls=%s",
                request_id,
                inputs["user_dept"],
                timings["total_ms"],
                timings.get("llm_ms"),
                timings.get("llm_calls"),
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
            logger.exception(
                "chat stream failed request_id=%s dept=%s upstream=%s",
                request_id,
                inputs["user_dept"],
                upstream,
            )
            yield _sse(
                {
                    "type": "error",
                    "code": "upstream_unavailable" if upstream else "internal_error",
                    "message": "llm upstream unavailable" if upstream else "internal error",
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
