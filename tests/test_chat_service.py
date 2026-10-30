"""Chat service unit tests: preparation, error mapping and cache handling."""

import json

import pytest
from fastapi import HTTPException

from src import chat_service
from src.cache import answer_cache
from src.schemas import ChatRequest


def _request(message="hello", dept="Tech", history=None):
    return ChatRequest(
        user_id="u1", user_dept=dept, message=message, history=history or []
    )


def test_prepare_builds_graph_inputs():
    prepared = chat_service.prepare(_request(), "chat")
    assert prepared.inputs["user_dept"] == "Tech"
    assert prepared.inputs["user_id"] == "u1"
    assert prepared.inputs["messages"][-1].content == "hello"
    assert len(prepared.request_id) == 8


def test_prepare_rejects_invalid_department():
    with pytest.raises(HTTPException) as exc:
        chat_service.prepare(_request(dept="bad dept"), "chat")
    assert exc.value.status_code == 400
    assert exc.value.headers["X-Request-ID"]


def test_prepare_rejects_during_maintenance(monkeypatch):
    monkeypatch.setattr(chat_service, "in_maintenance", lambda: True)
    with pytest.raises(HTTPException) as exc:
        chat_service.prepare(_request(), "chat_stream")
    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "index rebuilding"


@pytest.mark.parametrize(
    "exc, status, code",
    [
        (ConnectionRefusedError("nope"), 503, "vector_store_unavailable"),
        # Timeouts are now their own class (504) rather than a generic 500.
        (TimeoutError("slow"), 504, "timeout"),
        (RuntimeError("bug"), 500, "internal_error"),
    ],
)
def test_classify_error(exc, status, code):
    assert chat_service.classify_error(exc) == (status, code, chat_service.classify_error(exc)[2])


def test_is_store_error_detects_connection_errors():
    assert chat_service.is_store_error(ConnectionRefusedError("x"))
    assert not chat_service.is_store_error(RuntimeError("x"))


def test_cache_lookup_and_store_roundtrip():
    answer_cache.clear()
    request = _request("Cache roundtrip?")
    assert chat_service.cache_lookup(request, {}) is None

    from langchain_core.messages import AIMessage

    final_state = {
        "messages": [AIMessage(content="cached answer")],
        "sources": [{"source": "Tech/a.md"}],
    }
    chat_service.cache_store(request, {}, final_state, {"llm_calls": 1})

    hit = chat_service.cache_lookup(request, {})
    assert hit["response"] == "cached answer"
    assert hit["sources"] == [{"source": "Tech/a.md"}]
    answer_cache.clear()


def test_cache_store_skips_tool_answers():
    answer_cache.clear()
    from langchain_core.messages import AIMessage

    request = _request("Create a ticket")
    chat_service.cache_store(
        request, {}, {"messages": [AIMessage(content="TICK-1")]}, {"llm_calls": 2}
    )
    assert chat_service.cache_lookup(request, {}) is None


def test_cache_bypass_headers():
    assert chat_service.cache_bypassed({"cache-control": "no-cache"})
    assert chat_service.cache_bypassed({"x-no-cache": "1"})
    assert not chat_service.cache_bypassed({"cache-control": "max-age=60"})


def test_cache_store_skips_multi_turn():
    answer_cache.clear()
    from langchain_core.messages import AIMessage

    request = _request(
        "follow up", history=[{"role": "user", "content": "prior question"}]
    )
    chat_service.cache_store(
        request, {}, {"messages": [AIMessage(content="x")]}, {"llm_calls": 1}
    )
    assert chat_service.cache_lookup(request, {}) is None


def test_run_records_failure_and_raises_chat_failure(monkeypatch):
    answer_cache.clear()

    class Boom:
        def invoke(self, *args, **kwargs):
            raise ConnectionRefusedError("milvus down")

    monkeypatch.setattr(chat_service, "agent_app", Boom())
    with pytest.raises(chat_service.ChatFailure) as exc:
        chat_service.run(_request("fail please"), {}, endpoint="chat")
    assert exc.value.status_code == 503
    assert exc.value.code == "vector_store_unavailable"
    assert exc.value.request_id


def test_stream_emits_error_frame(monkeypatch):
    answer_cache.clear()

    class Boom:
        def stream(self, *args, **kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover - keeps this a generator

    monkeypatch.setattr(chat_service, "agent_app", Boom())
    frames = list(chat_service.open_stream(_request("stream fail"), {})[1])
    assert frames
    event = json.loads(frames[-1][6:])
    assert event["type"] == "error"
    assert event["code"] == "internal_error"


def test_stream_cache_hit_skips_graph(monkeypatch):
    answer_cache.clear()
    request = _request("stream cached?")

    class NoGraph:
        def stream(self, *args, **kwargs):
            raise AssertionError("graph must not run on a cache hit")

    monkeypatch.setattr(chat_service, "agent_app", NoGraph())
    from langchain_core.messages import AIMessage

    chat_service.cache_store(
        request, {}, {"messages": [AIMessage(content="cached!")], "sources": []},
        {"llm_calls": 1},
    )
    frames = list(chat_service.open_stream(request, {})[1])
    events = [json.loads(frame[6:]) for frame in frames]
    assert events[0]["type"] == "token"
    assert events[0]["content"] == "cached!"
    assert events[-1]["type"] == "done"
    answer_cache.clear()
