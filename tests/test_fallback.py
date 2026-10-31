"""LLM failure fallback: answer from retrieved excerpts, never cache it."""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage

from src import agent as agent_module
from src import chat_service
from src.cache import answer_cache
from src.config import settings
from src.schemas import ChatRequest


class Boom:
    def invoke(self, _messages):
        raise ConnectionError("llm unreachable")

    def stream(self, _messages):  # pragma: no cover - not used here
        raise ConnectionError("llm unreachable")
        yield  # noqa: B901


def _engine_with(docs):
    class Engine:
        @property
        def embeddings(self):
            raise AssertionError("not used")

        def search(self, *_args, **_kwargs):
            return docs

    return Engine()


def _doc(text="Rotate every 90 days.", source="Tech/security_policy.pdf", page=1):
    return Document(
        page_content=text,
        metadata={
            "source": source, "dept": "Tech", "kind": "pdf", "page": page,
            "slide": 0, "sheet": "", "title": "",
        },
    )


def test_fallback_answer_lists_excerpts(monkeypatch):
    monkeypatch.setattr(agent_module, "get_engine", lambda: _engine_with([_doc()]))
    monkeypatch.setattr(agent_module, "llm", Boom())
    state = {
        "messages": [HumanMessage(content="What are the password rules?")],
        "user_id": "u1",
        "user_dept": "Tech",
        "user_depts": ["Tech"],
    }
    update = agent_module.call_model(state)
    assert update["degraded"] is True
    assert update["degraded_reason"] == "llm_unavailable"
    text = update["messages"][0].content
    assert "模型服务暂时不可用" in text
    assert "security_policy.pdf" in text
    assert "Rotate every 90 days." in text
    assert update["sources"][0]["source"] == "Tech/security_policy.pdf"


def test_fallback_disabled_raises(monkeypatch):
    monkeypatch.setattr(agent_module, "get_engine", lambda: _engine_with([_doc()]))
    monkeypatch.setattr(agent_module, "llm", Boom())
    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", False)
    try:
        with pytest.raises(ConnectionError):
            agent_module.call_model(
                {
                    "messages": [HumanMessage(content="q")],
                    "user_id": "u1",
                    "user_dept": "Tech",
                    "user_depts": ["Tech"],
                }
            )
    finally:
        monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", True)


def test_fallback_without_excerpts_raises(monkeypatch):
    monkeypatch.setattr(agent_module, "get_engine", lambda: _engine_with([]))
    monkeypatch.setattr(agent_module, "llm", Boom())
    with pytest.raises(ConnectionError):
        agent_module.call_model(
            {
                "messages": [HumanMessage(content="q")],
                "user_id": "u1",
                "user_dept": "Tech",
                "user_depts": ["Tech"],
            }
        )


def test_fallback_after_tool_raises(monkeypatch):
    from langchain_core.messages import ToolMessage

    monkeypatch.setattr(agent_module, "get_engine", lambda: _engine_with([_doc()]))
    monkeypatch.setattr(agent_module, "llm", Boom())
    with pytest.raises(ConnectionError):
        agent_module.call_model(
            {
                "messages": [
                    HumanMessage(content="q"),
                    AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}]),
                    ToolMessage(content="done", tool_call_id="1"),
                ],
                "user_id": "u1",
                "user_dept": "Tech",
                "user_depts": ["Tech"],
            }
        )


def test_run_returns_degraded_and_skips_cache(monkeypatch):
    answer_cache.clear()
    monkeypatch.setattr(agent_module, "get_engine", lambda: _engine_with([_doc()]))
    monkeypatch.setattr(agent_module, "llm", Boom())
    monkeypatch.setattr(chat_service, "agent_app", agent_module.app)

    request = ChatRequest(user_id="u1", user_dept="Tech", message="password rules?")
    result = chat_service.run(request, {})
    assert result.degraded is True
    assert result.degraded_reason == "llm_unavailable"
    assert result.timings["degraded"] is True
    assert result.sources
    assert answer_cache.stats()["entries"] == 0


def test_stream_emits_degraded_done(monkeypatch):
    answer_cache.clear()
    monkeypatch.setattr(agent_module, "get_engine", lambda: _engine_with([_doc()]))
    monkeypatch.setattr(agent_module, "llm", Boom())
    monkeypatch.setattr(chat_service, "agent_app", agent_module.app)

    request = ChatRequest(user_id="u1", user_dept="Tech", message="password rules?")
    _, frames = chat_service.open_stream(request, {})
    events = [json.loads(frame[6:]) for frame in frames]
    done = [event for event in events if event["type"] == "done"]
    assert done and done[0]["degraded"] is True
    assert done[0]["degraded_reason"] == "llm_unavailable"
    assert answer_cache.stats()["entries"] == 0


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    (tmp_path / "data").mkdir()
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client


def test_api_surfaces_degraded(client, monkeypatch):
    monkeypatch.setattr(agent_module, "get_engine", lambda: _engine_with([_doc()]))
    monkeypatch.setattr(agent_module, "llm", Boom())
    response = client.post(
        "/chat",
        json={"user_id": "u1", "user_dept": "Tech", "message": "password rules?"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["degraded_reason"] == "llm_unavailable"
    assert body["sources"]
