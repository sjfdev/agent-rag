from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import time
from typing import Any, Dict, List, Optional
from src.agent import app as agent_app
from src.config import settings
from src.engine import kb_engine
from langchain_core.messages import HumanMessage, AIMessage

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

class ChatRequest(BaseModel):
    user_id: str
    user_dept: str
    message: str
    history: Optional[List[dict]] = []

class ChatResponse(BaseModel):
    response: str
    timings: Dict[str, Any] = {}

@app.on_event("startup")
async def startup_event():
    # Data is loaded by `python run_ingestion.py`; just warn if the store is empty.
    if kb_engine.is_empty():
        print("WARNING: vector store is empty. Run `python run_ingestion.py` first.")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Sync def: FastAPI runs it in a threadpool so long LLM calls don't block the event loop.
    try:
        started = time.perf_counter()

        # Convert history dicts to LangChain messages
        messages = []
        for msg in request.history:
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            else:
                messages.append(AIMessage(content=msg["content"]))
        
        # Add current message
        messages.append(HumanMessage(content=request.message))
        
        # Run LangGraph Agent
        inputs = {
            "messages": messages,
            "user_id": request.user_id,
            "user_dept": request.user_dept
        }
        
        final_state = agent_app.invoke(inputs)
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

        return ChatResponse(response=final_message, timings=timings)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
