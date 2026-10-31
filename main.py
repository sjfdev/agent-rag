import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from src import chat_service, metrics
from src.admin_api import _job_store
from src.admin_api import router as admin_router
from src.config import settings
from src.engine import kb_engine
from src.health import router as health_router
from src.schemas import ChatRequest, ChatResponse
from src.validation import InvalidDepartment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent-rag")

app = FastAPI(title="Enterprise KB Agent API")


def _cors_origins() -> list:
    raw = (settings.CORS_ORIGINS or "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


if _cors_origins():
    # Only the public chat/metadata endpoints are exposed. The admin API stays
    # same-origin because it can mutate the knowledge base.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-API-Key"],
        expose_headers=["X-Request-ID", "X-Cache"],
        max_age=600,
    )

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui/")


@app.exception_handler(InvalidDepartment)
async def _invalid_department_handler(request: Request, exc: InvalidDepartment):
    # Admin routes validate department names directly and rely on this mapping.
    return JSONResponse(
        status_code=400,
        content={"detail": "invalid department name"},
    )


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


@app.on_event("startup")
async def startup_event():
    # Jobs that were mid-flight when the process died are marked failed so the
    # console does not show a permanently "running" task after a restart.
    try:
        from src.jobs import recover_interrupted_jobs

        recovered = recover_interrupted_jobs(_job_store)
        if recovered:
            logger.warning("marked %d interrupted ingestion job(s) as failed", recovered)
    except Exception:
        logger.warning("could not recover interrupted jobs", exc_info=True)

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


def require_chat_key(http_request: Request) -> None:
    """Optional API key for the chat endpoints (open when unset)."""
    expected = settings.CHAT_API_KEY
    if not expected:
        return
    provided = (
        http_request.headers.get("x-api-key")
        or http_request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid api key")


@app.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    response: Response,
    http_request: Request,
    _: None = Depends(require_chat_key),
):
    # Sync def: FastAPI runs it in a threadpool so LLM calls don't block the loop.
    try:
        result = chat_service.run(request, http_request.headers, endpoint="chat")
    except chat_service.ChatFailure as failure:
        raise HTTPException(
            status_code=failure.status_code,
            detail={
                "error": failure.code,
                "message": failure.message,
                "request_id": failure.request_id,
            },
            headers={"X-Request-ID": failure.request_id},
        )

    response.headers["X-Request-ID"] = result.request_id
    response.headers["X-Cache"] = "hit" if result.timings.get("cached") else "miss"
    return result


@app.post("/chat/stream")
def chat_stream(
    request: ChatRequest,
    http_request: Request,
    _: None = Depends(require_chat_key),
):
    """Same as /chat but streams tokens as Server-Sent Events.

    Events: {"type":"token","content":...} ... {"type":"done","sources":[...],"timings":{...}}
    """
    request_id, frames = chat_service.open_stream(
        request, http_request.headers, endpoint="chat_stream"
    )
    return StreamingResponse(
        frames,
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
