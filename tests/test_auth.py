"""Optional /chat API key and multi-department request handling."""

import pytest
from fastapi.testclient import TestClient

from src.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "CHAT_API_KEY", "")
    (tmp_path / "data").mkdir()
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client


def test_chat_is_open_without_api_key(client):
    resp = client.post(
        "/chat", json={"user_id": "u1", "user_dept": "Tech", "message": "hi"}
    )
    # No LLM is configured in tests, but the request must not be rejected at auth.
    assert resp.status_code != 401


def test_chat_requires_api_key_when_configured(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setattr(settings, "CHAT_API_KEY", "k3y")
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    (tmp_path / "d").mkdir()
    import main

    importlib.reload(main)
    with TestClient(main.app) as c:
        body = {"user_id": "u1", "user_dept": "Tech", "message": "hi"}
        assert c.post("/chat", json=body).status_code == 401
        assert c.post("/chat", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.post("/chat", json=body, headers={"X-API-Key": "k3y"}).status_code != 401
        assert (
            c.post("/chat", json=body, headers={"Authorization": "Bearer k3y"}).status_code
            != 401
        )
        assert c.post("/chat/stream", json=body).status_code == 401


def test_request_accepts_multiple_departments(client):
    body = {
        "user_id": "u1",
        "user_depts": ["Tech", "Finance"],
        "message": "hi",
        "user_groups": ["exec"],
    }
    resp = client.post("/chat", json=body)
    assert resp.status_code != 400


def test_request_rejects_invalid_department_in_list(client):
    body = {"user_id": "u1", "user_depts": ["Tech", "bad dept"], "message": "hi"}
    resp = client.post("/chat", json=body)
    assert resp.status_code == 400


def test_request_requires_at_least_one_department(client):
    resp = client.post("/chat", json={"user_id": "u1", "message": "hi"})
    assert resp.status_code == 400
