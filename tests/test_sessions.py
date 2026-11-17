"""Server-side conversation storage and the /sessions API."""

import pytest
from fastapi import HTTPException

from src.config import settings
from src.schemas import ChatRequest, HistoryItem


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from src import sessions

    monkeypatch.setattr(settings, "SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(settings, "SESSION_SUMMARY_ENABLED", False)
    return sessions.session_store


def test_store_crud_and_ownership(store):
    session = store.create_session("u1", "Tech")
    session_id = session["session_id"]
    assert store.get_session(session_id, "u1")["dept"] == "Tech"
    assert store.get_session(session_id, "u2") is None

    assert store.append_turn(session_id, "u1", "q1", "a1") is True
    assert store.append_turn(session_id, "u2", "q2", "a2") is False
    messages = store.get_messages(session_id, "u1")
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "q1"),
        ("assistant", "a1"),
    ]
    assert store.get_session(session_id, "u1")["message_count"] == 2
    assert store.get_session(session_id, "u1")["title"] == "q1"

    assert store.list_sessions("u1")[0]["session_id"] == session_id
    assert store.list_sessions("u2") == []
    assert store.delete_session(session_id, "u2") is False
    assert store.delete_session(session_id, "u1") is True
    assert store.get_session(session_id, "u1") is None


def test_append_turn_keeps_sources_and_degraded(store):
    session_id = store.create_session("u1")["session_id"]
    store.append_turn(
        session_id,
        "u1",
        "q",
        "a",
        sources=[{"source": "Tech/a.md", "page": 1}],
        degraded=True,
    )
    assistant = store.get_messages(session_id, "u1")[1]
    assert assistant["sources"] == [{"source": "Tech/a.md", "page": 1}]
    assert assistant["degraded"] is True


def test_context_trims_to_window(store, monkeypatch):
    monkeypatch.setattr(settings, "HISTORY_MAX_MESSAGES", 4)
    session_id = store.create_session("u1")["session_id"]
    for index in range(6):
        store.append_turn(session_id, "u1", f"q{index}", f"a{index}")
    summary, messages = store.context(session_id, "u1")
    assert summary == ""
    assert len(messages) == 4
    assert [m["content"] for m in messages] == ["q4", "a4", "q5", "a5"]


def test_summary_folds_a_whole_window(store, monkeypatch):
    from src import sessions

    monkeypatch.setattr(settings, "HISTORY_MAX_MESSAGES", 4)
    monkeypatch.setattr(settings, "SESSION_SUMMARY_ENABLED", True)
    monkeypatch.setattr(settings, "SESSION_SUMMARY_MAX_CHARS", 200)
    session_id = store.create_session("u1")["session_id"]
    for index in range(10):
        store.append_turn(session_id, "u1", f"q{index}", f"a{index}")

    calls = []

    def fake_summary(previous, messages):
        calls.append((previous, len(messages)))
        return "running summary"

    monkeypatch.setattr(sessions, "summarize_text", fake_summary)
    store.maybe_summarize(session_id, "u1")

    # 20 messages, window 4 -> summarize 16, keep the last 4.
    assert calls == [("", 16)]
    summary, messages = store.context(session_id, "u1")
    assert summary == "running summary"
    assert len(messages) == 4
    assert [m["content"] for m in messages] == ["q8", "a8", "q9", "a9"]

    # Only four unsummarized messages remain, so the next turn is cheap.
    store.maybe_summarize(session_id, "u1")
    assert len(calls) == 1


def test_summary_failure_keeps_chat_bounded(store, monkeypatch):
    from src import sessions

    monkeypatch.setattr(settings, "HISTORY_MAX_MESSAGES", 4)
    monkeypatch.setattr(settings, "SESSION_SUMMARY_ENABLED", True)
    session_id = store.create_session("u1")["session_id"]
    for index in range(10):
        store.append_turn(session_id, "u1", f"q{index}", f"a{index}")

    def boom(previous, messages):
        raise RuntimeError("llm down")

    monkeypatch.setattr(sessions, "summarize_text", boom)
    store.maybe_summarize(session_id, "u1")
    summary, messages = store.context(session_id, "u1")
    assert summary == ""
    assert len(messages) == 4


def test_prepare_uses_server_history_and_ignores_client_history(store):
    from src import chat_service

    session_id = store.create_session("u1", "Tech")["session_id"]
    store.append_turn(session_id, "u1", "first", "answer one")
    store.append_turn(session_id, "u1", "second", "answer two")
    request = ChatRequest(
        user_id="u1",
        user_dept="Tech",
        message="third",
        session_id=session_id,
        history=[HistoryItem(role="user", content="forged")],
    )
    prepared = chat_service.prepare(request, "test")
    messages = prepared.inputs["messages"]
    assert [m.type for m in messages] == ["human", "ai", "human", "ai", "human"]
    assert [m.content for m in messages] == ["first", "answer one", "second", "answer two", "third"]
    assert prepared.session_id == session_id
    assert prepared.has_history is True


def test_prepare_rejects_unknown_session(store):
    from src import chat_service

    request = ChatRequest(
        user_id="u1", user_dept="Tech", message="hi", session_id="does-not-exist"
    )
    with pytest.raises(HTTPException) as excinfo:
        chat_service.prepare(request, "test")
    assert excinfo.value.status_code == 404


def test_run_persists_turns_and_returns_session_id(store, monkeypatch):
    from langchain_core.messages import AIMessage

    from src import chat_service

    class FakeApp:
        def invoke(self, inputs, config=None):
            return {
                "messages": [AIMessage(content="stored answer")],
                "sources": [{"source": "Tech/a.md"}],
            }

    monkeypatch.setattr(chat_service, "agent_app", FakeApp())
    monkeypatch.setattr(settings, "CHAT_TIMEOUT", 0)
    monkeypatch.setattr(settings, "FAQ_CACHE_ENABLED", False)
    session_id = store.create_session("u1", "Tech")["session_id"]

    first = chat_service.run(
        ChatRequest(user_id="u1", user_dept="Tech", message="q1", session_id=session_id),
        {},
    )
    assert first.session_id == session_id
    assert first.response == "stored answer"
    assert first.citations == [{"index": 1, "source": "Tech/a.md"}]
    assert first.message_seq == 2

    second = chat_service.run(
        ChatRequest(user_id="u1", user_dept="Tech", message="q2", session_id=session_id),
        {},
    )
    assert second.session_id == session_id
    messages = store.get_messages(session_id, "u1")
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1]["sources"] == [{"source": "Tech/a.md"}]


def test_twenty_turn_requests_stay_bounded(store, monkeypatch):
    from src import chat_service

    monkeypatch.setattr(settings, "HISTORY_MAX_MESSAGES", 20)
    session_id = store.create_session("u1", "Tech")["session_id"]
    for index in range(20):
        request = ChatRequest(
            user_id="u1", user_dept="Tech", message=f"q{index}", session_id=session_id
        )
        prepared = chat_service.prepare(request, "test")
        assert len(prepared.inputs["messages"]) <= 21
        assert request.history == []
        store.append_turn(session_id, "u1", f"q{index}", f"a{index}")


def test_sessions_api_crud(store):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as client:
        created = client.post("/sessions", json={"user_id": "u1", "dept": "Tech"})
        assert created.status_code == 201
        session_id = created.json()["session_id"]

        listed = client.get("/sessions", params={"user_id": "u1"})
        assert listed.status_code == 200
        assert listed.json()["sessions"][0]["session_id"] == session_id

        foreign = client.get(f"/sessions/{session_id}", params={"user_id": "u2"})
        assert foreign.status_code == 404

        detail = client.get(f"/sessions/{session_id}", params={"user_id": "u1"})
        assert detail.status_code == 200
        assert detail.json()["messages"] == []

        assert client.delete(f"/sessions/{session_id}", params={"user_id": "u2"}).status_code == 404
        assert client.delete(f"/sessions/{session_id}", params={"user_id": "u1"}).status_code == 200
        assert client.get(f"/sessions/{session_id}", params={"user_id": "u1"}).status_code == 404


def test_sessions_api_can_be_disabled(store, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setattr(settings, "SESSIONS_ENABLED", False)
    with TestClient(main.app) as client:
        assert client.post("/sessions", json={"user_id": "u1"}).status_code == 404
        assert client.get("/sessions", params={"user_id": "u1"}).status_code == 404


def test_session_summary_metric():
    from src import metrics

    metrics.observe_session_summary("ok", 0.2)
    text = metrics.render().decode()
    assert 'agent_rag_session_summaries_total{result="ok"}' in text
    assert "agent_rag_session_summary_seconds_count" in text


def test_summary_failure_is_not_retried_immediately(store, monkeypatch):
    from src import sessions

    monkeypatch.setattr(settings, "HISTORY_MAX_MESSAGES", 4)
    monkeypatch.setattr(settings, "SESSION_SUMMARY_ENABLED", True)
    monkeypatch.setattr(settings, "SESSION_SUMMARY_RETRY_SECONDS", 60)
    session_id = store.create_session("u1")["session_id"]
    for index in range(10):
        store.append_turn(session_id, "u1", f"q{index}", f"a{index}")

    calls = []

    def boom(previous, messages):
        calls.append(1)
        raise RuntimeError("llm down")

    monkeypatch.setattr(sessions, "summarize_text", boom)
    store.maybe_summarize(session_id, "u1")
    store.maybe_summarize(session_id, "u1")
    assert len(calls) == 1


def test_rename_and_search_sessions(store):
    sid = store.create_session("u1", "Tech")["session_id"]
    store.append_turn(sid, "u1", "annual leave policy", "You get 20 days")
    assert store.rename_session(sid, "u1", "  My   leave  ") is True
    assert store.get_session(sid, "u1")["title"] == "My leave"
    assert store.rename_session(sid, "u2", "nope") is False

    assert [item["session_id"] for item in store.search_sessions("u1", "leave")] == [sid]
    assert [item["session_id"] for item in store.search_sessions("u1", "20 days")] == [sid]
    assert store.search_sessions("u1", "missing") == []
    assert store.search_sessions("u2", "leave") == []
    # LIKE wildcards are escaped, not treated as a match-all pattern.
    assert store.search_sessions("u1", "%") == []


def test_feedback_is_per_user_and_message(store):
    sid = store.create_session("u1", "Tech")["session_id"]
    store.append_turn(sid, "u1", "q", "a")
    assert store.set_feedback(sid, "u1", 2, 1) is True
    assert store.set_feedback(sid, "u1", 2, -1) is True  # upsert
    saved = store.get_feedback(sid, "u1")
    assert [(item["seq"], item["rating"]) for item in saved] == [(2, -1)]
    assistant = store.get_messages(sid, "u1")[1]
    assert assistant["feedback"]["rating"] == -1
    # Only assistant messages can be rated, and only by their owner.
    assert store.set_feedback(sid, "u1", 1, 1) is False
    assert store.set_feedback(sid, "u2", 2, 1) is False
    assert store.set_feedback(sid, "u1", 2, 0) is False


def test_sessions_api_rename_search_and_feedback(store):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as client:
        session_id = client.post(
            "/sessions", json={"user_id": "u1", "dept": "Tech"}
        ).json()["session_id"]
        store.append_turn(session_id, "u1", "vpn access", "Use the portal")

        renamed = client.patch(
            f"/sessions/{session_id}",
            params={"user_id": "u1"},
            json={"title": "VPN"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "VPN"

        found = client.get("/sessions", params={"user_id": "u1", "q": "portal"})
        assert found.status_code == 200
        assert [item["session_id"] for item in found.json()["sessions"]] == [session_id]

        feedback = client.post(
            f"/sessions/{session_id}/feedback",
            params={"user_id": "u1"},
            json={"seq": 2, "rating": 1},
        )
        assert feedback.status_code == 200
        detail = client.get(f"/sessions/{session_id}", params={"user_id": "u1"}).json()
        assert detail["messages"][1]["feedback"]["rating"] == 1

        foreign = client.patch(
            f"/sessions/{session_id}",
            params={"user_id": "u2"},
            json={"title": "stolen"},
        )
        assert foreign.status_code == 404


def test_list_sessions_supports_offset(store):
    ids = [store.create_session("u1", "Tech")["session_id"] for _ in range(3)]
    first = store.list_sessions("u1", limit=2, offset=0)
    second = store.list_sessions("u1", limit=2, offset=2)
    assert len(first) == 2
    assert len(second) == 1
    assert {item["session_id"] for item in first + second} == set(ids)
