"""A client disconnect must abort the upstream LLM call, not finish it.

Starlette cancels the response task when the client goes away (uvicorn
advertises ASGI spec 2.3, so `StreamingResponse` uses its disconnect listener).
The stream is an async generator, so that cancellation reaches the async graph
node and its `llm.ainvoke`. These tests assert the call is aborted and the
half-finished turn is neither cached nor persisted.
"""

import asyncio
import contextlib
import json

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from src import agent as agent_module
from src import chat_service, metrics, sessions
from src.cache import answer_cache
from src.config import settings
from src.schemas import ChatRequest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(settings, "SESSION_SUMMARY_ENABLED", False)
    return sessions.session_store


def _request(session_id=None, message="password rules?"):
    return ChatRequest(
        user_id="u1",
        user_dept="Tech",
        message=message,
        session_id=session_id,
    )


def _engine_with(docs):
    class Engine:
        def search(self, *_args, **_kwargs):
            return list(docs)

    return Engine()


def _doc():
    return Document(
        page_content="password rotation policy",
        metadata={"source": "Tech/a.md", "dept": "Tech"},
    )


def _stub_registry_engine(monkeypatch):
    """Keep `metrics.render()` from loading a real vector store."""
    import src.engine as engine_module

    class Stub:
        def live_count(self):
            return 1

        def hybrid_status(self):
            return {"engine": "off", "sparse_ready": False}

        def cache_status(self):
            return {"embeddings": {"entries": 0}}

    monkeypatch.setattr(engine_module, "kb_engine", Stub())


def test_cancel_aborts_upstream_llm_and_drops_turn(store, monkeypatch):
    answer_cache.clear()
    _stub_registry_engine(monkeypatch)
    monkeypatch.setattr(agent_module, "get_engine", lambda: _engine_with([_doc()]))
    aborted = {"count": 0}

    class SlowLLM:
        async def ainvoke(self, _messages):
            try:
                await asyncio.sleep(5)
                return AIMessage(content="late answer")
            finally:
                aborted["count"] += 1

    monkeypatch.setattr(agent_module, "llm", SlowLLM())

    session_id = store.create_session("u1", "Tech")["session_id"]
    request = _request(session_id=session_id)
    prepared = chat_service.prepare(request, "chat_stream")
    frames = chat_service._astream(prepared, request, {})

    async def scenario():
        async def consume():
            async for _frame in frames:
                pass

        task = asyncio.create_task(consume())
        # The graph is now waiting inside `llm.ainvoke`; the user closes the tab.
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert aborted["count"] == 1, "the in-flight LLM call must be cancelled"
    # Half an answer is not an answer: no cache entry, no session turn.
    assert answer_cache.stats()["entries"] == 0
    assert store.get_messages(session_id, "u1") == []
    assert (
        'agent_rag_chat_cancelled_total{endpoint="chat_stream"}'
        in metrics.render().decode()
    )


def test_aclose_after_first_token_records_cancelled(monkeypatch):
    answer_cache.clear()
    _stub_registry_engine(monkeypatch)
    upstream = {"cancelled": False}

    class SlowStream:
        async def astream(self, *args, **kwargs):
            try:
                yield (
                    "messages",
                    (AIMessage(content="first"), {"langgraph_node": "agent"}),
                )
                await asyncio.sleep(5)
                yield ("values", {"messages": [], "sources": []})
            finally:
                upstream["cancelled"] = True

    monkeypatch.setattr(chat_service, "agent_async_app", SlowStream())

    request = _request()
    prepared = chat_service.prepare(request, "chat_stream")
    frames = chat_service._astream(prepared, request, {})

    async def scenario():
        first = await frames.__anext__()
        event = json.loads(first[6:])
        assert event == {"type": "token", "content": "first"}
        await frames.aclose()
        # Let the producer task receive the cancellation we queued in `finally`.
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert upstream["cancelled"] is True, "the producer must cancel the graph"
    assert answer_cache.stats()["entries"] == 0
    assert (
        'agent_rag_chat_cancelled_total{endpoint="chat_stream"}'
        in metrics.render().decode()
    )
