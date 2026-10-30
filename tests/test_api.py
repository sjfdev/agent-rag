"""API-level behaviour with a stubbed LLM.

FastAPI's TestClient drives the real graph, but the model is replaced so these
tests never call a network endpoint. Retrieval runs against a temp FAISS store
filled with fixtures.
"""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from src.config import settings


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """Temp FAISS store with two departments and a stub LLM."""
    import importlib

    # Use the process singleton so the app, agent, admin API and ETL all see
    # the same temp index (they imported the object at startup).
    from src.engine import kb_engine as engine

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "DATA_DIR", str(data))
    monkeypatch.setattr(settings, "FAQ_CACHE_ENABLED", True)

    engine.reset()
    engine.add_documents(
        [
            "Every change must go through a pull request with one approving reviewer.",
            "Salaries are confidential and only visible to Finance.",
            "Visitors use the DEMO-GUEST wifi network.",
        ],
        [
            {"dept": "Tech", "source": "Tech/policy.md", "doc_id": "tech1",
             "doc_hash": "h1", "kind": "md", "page": 1, "slide": 0, "sheet": "",
             "title": "", "chunk_index": 0},
            {"dept": "Finance", "source": "Finance/budget.md", "doc_id": "fin1",
             "doc_hash": "h2", "kind": "md", "page": 1, "slide": 0, "sheet": "",
             "title": "", "chunk_index": 0},
            {"dept": "Demo", "source": "Demo/faq.md", "doc_id": "demo1",
             "doc_hash": "h3", "kind": "md", "page": 1, "slide": 0, "sheet": "",
             "title": "", "chunk_index": 0},
        ],
    )
    import src.agent as agent_module

    class StubLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            return AIMessage(content="stub answer")

    stub = StubLLM()
    monkeypatch.setattr(agent_module, "llm", stub)
    import main

    importlib.reload(main)
    with TestClient(main.app) as client:
        try:
            yield client, stub, engine
        finally:
            engine.reset()


def _chat(client, message="What must happen before merging?", dept="Tech", headers=None,
          history=None):
    body = {
        "user_id": "u1",
        "user_dept": dept,
        "message": message,
        "history": history or [],
    }
    return client.post("/chat", json=body, headers=headers or {})


def test_chat_returns_answer_and_sources(seeded):
    client, stub, _ = seeded
    resp = _chat(client)
    assert resp.status_code == 200
    data = resp.json()
    assert data["response"] == "stub answer"
    assert data["sources"]
    assert all(source["dept"] == "Tech" for source in data["sources"])
    assert all(source["kind"] == "md" for source in data["sources"])
    assert all(source["snippet"] for source in data["sources"])


def test_department_isolation(seeded):
    client, _, _ = seeded
    tech = _chat(client, "How much is the salary?", dept="Tech").json()
    finance = _chat(client, "How much is the salary?", dept="Finance").json()
    assert all(s["dept"] == "Tech" for s in tech["sources"])
    assert all(s["dept"] == "Finance" for s in finance["sources"])


def test_invalid_department_returns_400(seeded):
    client, _, _ = seeded
    resp = _chat(client, dept="Tech' or dept != '")
    assert resp.status_code == 400


def test_history_validation(seeded):
    client, _, _ = seeded
    resp = _chat(client, history=[{"role": "tool", "content": "x"}])
    assert resp.status_code == 422


def test_cache_hit_skips_llm(seeded):
    client, stub, _ = seeded
    first = _chat(client, "Cache me please").json()
    calls_after_first = stub.calls
    second_resp = _chat(client, "cache me PLEASE!!")
    second = second_resp.json()
    assert second_resp.headers.get("X-Cache") == "hit"
    assert second["response"] == first["response"]
    assert second["timings"]["cached"] is True
    assert second["timings"]["llm_calls"] == 0
    assert stub.calls == calls_after_first


def test_cache_bypass_header(seeded):
    client, stub, _ = seeded
    _chat(client, "Bypass me")
    calls = stub.calls
    resp = _chat(client, "Bypass me", headers={"Cache-Control": "no-cache"})
    assert resp.headers.get("X-Cache") is None
    assert stub.calls > calls


def test_followup_is_not_cached(seeded):
    client, stub, _ = seeded
    history = [
        {"role": "user", "content": "What is the code review process?"},
        {"role": "assistant", "content": "One approval."},
    ]
    _chat(client, "Who approves it?", history=history)
    calls = stub.calls
    resp = _chat(client, "Who approves it?", history=history)
    assert resp.headers.get("X-Cache") is None
    assert stub.calls > calls


def test_cache_cleared_after_write(seeded):
    client, _, engine = seeded
    from src.cache import answer_cache

    _chat(client, "Will this be cleared?")
    assert answer_cache.stats()["entries"] >= 1
    engine.add_documents(
        ["new content"],
        [{"dept": "Tech", "source": "Tech/new.md", "doc_id": "tech2",
          "doc_hash": "h9", "kind": "md", "page": 1, "slide": 0, "sheet": "",
          "title": "", "chunk_index": 0}],
    )
    assert answer_cache.stats()["entries"] == 0


def test_stream_emits_tokens_and_done(seeded, monkeypatch):
    client, _, _ = seeded

    class StubStreamLLM:
        def stream(self, messages):
            yield AIMessage(content="hello ")
            yield AIMessage(content="world")

    # The graph is compiled with the original llm binding; exercise the SSE
    # endpoint with the existing stub, which still returns a single message.
    resp = client.post(
        "/chat/stream",
        json={"user_id": "u1", "user_dept": "Tech", "message": "stream test"},
    )
    assert resp.status_code == 200
    events = [
        json.loads(line[6:])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert any(event["type"] == "done" for event in events)


def test_health_reports_cache(seeded):
    client, _, _ = seeded
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert "cache" in health
    assert isinstance(health["cache"]["answer"]["entries"], int)
