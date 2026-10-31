"""Shared error classification and the request deadline."""

import time

import pytest

from src import chat_service
from src.config import settings
from src.errors import classify_error, is_store_error, is_upstream_error
from src.schemas import ChatRequest


def _request(message="hi"):
    return ChatRequest(user_id="u1", user_dept="Tech", message=message)


def test_store_errors():
    assert is_store_error(ConnectionRefusedError("x")) is True
    assert is_store_error(RuntimeError("x")) is False


def test_upstream_errors():
    assert is_upstream_error(TimeoutError("slow")) is False
    assert is_upstream_error(RuntimeError("x")) is False


def test_classify_timeout():
    assert classify_error(TimeoutError("slow"))[0] == 504
    assert classify_error(TimeoutError("slow"))[1] == "timeout"


def test_classify_store_and_internal():
    assert classify_error(ConnectionError("down"))[:2] == (
        503,
        "vector_store_unavailable",
    )
    assert classify_error(RuntimeError("bug"))[:2] == (500, "internal_error")


def test_chat_timeout_raises_chat_failure(monkeypatch):
    class Slow:
        def invoke(self, *args, **kwargs):
            time.sleep(1.5)
            return {"messages": []}

    monkeypatch.setattr(chat_service, "agent_app", Slow())
    monkeypatch.setattr(settings, "CHAT_TIMEOUT", 1)
    try:
        with pytest.raises(chat_service.ChatFailure) as exc:
            chat_service.run(_request("slow"), {})
        assert exc.value.status_code == 504
        assert exc.value.code == "timeout"
    finally:
        monkeypatch.setattr(settings, "CHAT_TIMEOUT", 120)


def test_chat_timeout_disabled_invokes_directly(monkeypatch):
    called = {"n": 0}

    class Fast:
        def invoke(self, *args, **kwargs):
            called["n"] += 1
            from langchain_core.messages import AIMessage

            return {"messages": [AIMessage(content="ok")], "sources": []}

    monkeypatch.setattr(chat_service, "agent_app", Fast())
    monkeypatch.setattr(settings, "CHAT_TIMEOUT", 0)
    try:
        result = chat_service.run(_request(), {})
        assert result.response == "ok"
        assert called["n"] == 1
    finally:
        monkeypatch.setattr(settings, "CHAT_TIMEOUT", 120)


def test_stream_deadline_emits_timeout_error(monkeypatch):
    class Slow:
        def stream(self, *args, **kwargs):
            from langchain_core.messages import AIMessage

            yield ("messages", (AIMessage(content="first"), {"langgraph_node": "agent"}))
            time.sleep(1.2)
            yield ("messages", (AIMessage(content="late"), {"langgraph_node": "agent"}))
            yield ("values", {"messages": [], "sources": []})

    monkeypatch.setattr(chat_service, "agent_app", Slow())
    monkeypatch.setattr(settings, "CHAT_TIMEOUT", 1)
    try:
        _, frames = chat_service.open_stream(_request("slow"), {})
        import json

        events = [json.loads(frame[6:]) for frame in frames]
        assert events[0]["content"] == "first"
        assert events[-1]["type"] == "error"
        assert events[-1]["code"] == "timeout"
    finally:
        monkeypatch.setattr(settings, "CHAT_TIMEOUT", 120)


def test_rate_limit_identity_uses_forwarded_ip(monkeypatch):
    assert (
        chat_service._client_key("anon", {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
        == "ip:1.2.3.4"
    )
    assert chat_service._client_key("anon", {"x-real-ip": "9.9.9.9"}) == "ip:9.9.9.9"
    assert chat_service._client_key("anon", {}) == "user:anon"
