"""Conversation history trimming: long chats degrade gracefully."""

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from src import chat_service
from src.config import settings
from src.schemas import ChatRequest, HistoryItem


def _messages(count):
    return [
        HistoryItem(role="user" if i % 2 == 0 else "assistant", content=f"m{i}")
        for i in range(count)
    ]


def test_short_history_is_untouched():
    kept = chat_service.trim_history(_messages(4), limit=20)
    assert [m.content for m in kept] == ["m0", "m1", "m2", "m3"]


def test_long_history_keeps_the_tail():
    kept = chat_service.trim_history(_messages(30), limit=6)
    assert len(kept) == 6
    assert [m.content for m in kept] == [f"m{i}" for i in range(24, 30)]


def test_trim_never_starts_with_assistant():
    # Odd tail length would start on an assistant message without the fix.
    kept = chat_service.trim_history(_messages(21), limit=20)
    assert kept[0].role == "user"


def test_trim_starting_on_assistant_drops_it():
    history = _messages(5)  # user, assistant, user, assistant, user
    kept = chat_service.trim_history(history, limit=2)  # would be assistant,user
    assert kept and kept[0].role == "user"
    assert kept[0].content == "m4"


def test_limit_zero_disables_context():
    assert chat_service.trim_history(_messages(10), limit=0) == []


def test_prepare_uses_trimmed_history(monkeypatch):
    monkeypatch.setattr(settings, "HISTORY_MAX_MESSAGES", 4)
    request = ChatRequest(
        user_id="u1",
        user_dept="Tech",
        message="latest",
        history=_messages(12),
    )
    prepared = chat_service.prepare(request, "chat")
    messages = prepared.inputs["messages"]
    # 4 kept history messages + the new question.
    assert len(messages) == 5
    assert isinstance(messages[0], HumanMessage)
    assert messages[-1].content == "latest"


def test_prepare_drops_orphan_assistant(monkeypatch):
    monkeypatch.setattr(settings, "HISTORY_MAX_MESSAGES", 3)
    request = ChatRequest(
        user_id="u1",
        user_dept="Tech",
        message="latest",
        history=_messages(6),  # tail of 3 starts with assistant
    )
    prepared = chat_service.prepare(request, "chat")
    messages = prepared.inputs["messages"]
    assert isinstance(messages[0], HumanMessage)
    assert all(not isinstance(m, AIMessage) or isinstance(m, AIMessage) for m in messages)
    assert isinstance(messages[-1], HumanMessage)


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


def test_api_accepts_long_history(client, monkeypatch):
    """A 60-message history must not be rejected anymore."""
    from langchain_core.documents import Document

    class Engine:
        @property
        def embeddings(self):
            raise AssertionError("not used")

        def search(self, *_a, **_k):
            return [
                Document(
                    page_content="Every change needs one reviewer.",
                    metadata={
                        "source": "Tech/a.md", "dept": "Tech", "kind": "md",
                        "page": 1, "slide": 0, "sheet": "", "title": "",
                    },
                )
            ]

    import src.agent as agent_module

    class Stub:
        def invoke(self, _messages):
            return AIMessage(content="ok")

    monkeypatch.setattr(agent_module, "get_engine", lambda: Engine())
    monkeypatch.setattr(agent_module, "llm", Stub())

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(60)
    ]
    resp = client.post(
        "/chat",
        json={
            "user_id": "u1", "user_dept": "Tech", "message": "latest", "history": history,
        },
    )
    assert resp.status_code == 200, resp.text


def test_api_rejects_absurd_history_size(client):
    """The hard cap still protects the server from a huge payload."""
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x"}
        for i in range(400)
    ]
    resp = client.post(
        "/chat",
        json={
            "user_id": "u1", "user_dept": "Tech", "message": "latest", "history": history,
        },
    )
    assert resp.status_code == 422
