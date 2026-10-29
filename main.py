import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from src.agent import app as agent_app
from src.config import settings
from src.engine import kb_engine
from src.validation import InvalidDepartment, validate_dept

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
    timings: Dict[str, Any] = {}


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
    started = time.perf_counter()

    try:
        safe_dept = validate_dept(request.user_dept)
    except InvalidDepartment:
        logger.warning(
            "rejected invalid dept request_id=%s dept=%r", request_id, request.user_dept
        )
        raise HTTPException(status_code=400, detail="invalid department name")

    messages = []
    for item in request.history:
        cls = HumanMessage if item.role == "user" else AIMessage
        messages.append(cls(content=item.content))
    messages.append(HumanMessage(content=request.message))

    inputs = {
        "messages": messages,
        "user_id": request.user_id,
        "user_dept": safe_dept,
    }

    try:
        final_state = agent_app.invoke(
            inputs, config={"recursion_limit": settings.AGENT_RECURSION_LIMIT}
        )
        final_message = final_state["messages"][-1].content

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

        logger.info(
            "chat ok request_id=%s dept=%s total_ms=%s llm_ms=%s llm_calls=%s",
            request_id,
            safe_dept,
            timings["total_ms"],
            timings.get("llm_ms"),
            timings.get("llm_calls"),
        )
        return ChatResponse(response=final_message, timings=timings)
    except Exception:
        logger.exception(
            "chat failed request_id=%s dept=%s", request_id, safe_dept
        )
        raise HTTPException(
            status_code=500,
            detail={"error": "internal error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
