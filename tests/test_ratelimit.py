"""Sliding-window rate limiter and its integration with the chat endpoints."""

import time

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.ratelimit import RateLimiter


def test_disabled_limiter_allows_everything():
    limiter = RateLimiter(limit=0)
    for _ in range(100):
        assert limiter.allow("u")[0] is True


def test_limit_is_enforced_within_window():
    limiter = RateLimiter(limit=2, window=60)
    assert limiter.allow("u")[0] is True
    assert limiter.allow("u")[0] is True
    allowed, retry = limiter.allow("u")
    assert allowed is False
    assert 0 < retry <= 60


def test_limit_is_per_key():
    limiter = RateLimiter(limit=1, window=60)
    assert limiter.allow("a")[0] is True
    assert limiter.allow("b")[0] is True
    assert limiter.allow("a")[0] is False


def test_window_expiry_frees_slots():
    limiter = RateLimiter(limit=1, window=0.05)
    assert limiter.allow("u")[0] is True
    assert limiter.allow("u")[0] is False
    time.sleep(0.08)
    assert limiter.allow("u")[0] is True


def test_reset_and_stats():
    limiter = RateLimiter(limit=5, window=60)
    limiter.allow("a")
    limiter.allow("b")
    assert limiter.stats() == {"keys": 2, "events": 2}
    limiter.reset()
    assert limiter.stats() == {"keys": 0, "events": 0}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 2)
    (tmp_path / "data").mkdir()
    import main

    importlib.reload(main)
    from src import chat_service

    chat_service.ratelimiter().reset()
    with TestClient(main.app) as test_client:
        yield test_client
    chat_service.ratelimiter().reset()
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 0)


def test_chat_returns_429_with_retry_after(client):
    body = {"user_id": "rl-user", "user_dept": "Tech", "message": "hi"}
    first = client.post("/chat", json=body)
    second = client.post("/chat", json=body)
    third = client.post("/chat", json=body)
    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code == 429
    assert third.headers["Retry-After"].isdigit()
    payload = third.json()["detail"]
    assert payload["error"] == "rate_limited"
    assert payload["request_id"]


def test_rate_limit_is_per_user(client):
    alice = {"user_id": "alice", "user_dept": "Tech", "message": "hi"}
    bob = {"user_id": "bob", "user_dept": "Tech", "message": "hi"}
    for _ in range(2):
        client.post("/chat", json=alice)
    assert client.post("/chat", json=alice).status_code == 429
    assert client.post("/chat", json=bob).status_code != 429


def test_stream_is_rate_limited(client):
    body = {"user_id": "rl-stream", "user_dept": "Tech", "message": "hi"}
    for _ in range(2):
        client.post("/chat/stream", json=body)
    assert client.post("/chat/stream", json=body).status_code == 429
