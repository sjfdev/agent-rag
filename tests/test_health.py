"""Health endpoint states: ok, degraded, down, maintenance and LLM probe."""

import pytest
from fastapi.testclient import TestClient

from src import health, runtime
from src.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    (tmp_path / "data").mkdir()
    health._probe_cache["at"] = 0.0
    health._probe_cache["result"] = None
    runtime.end_maintenance()
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client
    runtime.end_maintenance()


def _seed(dept="Tech", count=1):
    from src.engine import kb_engine

    docs = ["first chunk", "second chunk"][:count]
    metas = [
        {"dept": dept, "source": f"{dept}/{i}.md", "doc_id": f"{dept}-{i}",
         "doc_hash": f"h{i}", "kind": "md", "page": 1, "slide": 0,
         "sheet": "", "title": "", "chunk_index": 0}
        for i in range(count)
    ]
    kb_engine.add_documents(docs, metas)


def test_health_ok_with_data(client):
    _seed(count=2)
    resp = client.get("/health")
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] in {"ok", "degraded"}
    assert body["vector_store"]["chunks"] >= 2
    assert body["vector_store"]["ok"] is True
    assert body["maintenance"] is False
    assert "cache" in body and "hybrid" in body


def test_health_down_when_empty(client):
    from src.engine import kb_engine

    kb_engine.reset()
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "down"


def test_health_degraded_when_maintenance(client):
    _seed()
    runtime.begin_maintenance()
    try:
        body = client.get("/health").json()
        assert body["maintenance"] is True
        assert body["status"] == "degraded"
    finally:
        runtime.end_maintenance()


def test_health_probe_ok(client, monkeypatch):
    _seed()

    class FakeResponse:
        status_code = 200

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse())
    body = client.get("/health?probe=1").json()
    assert body["llm"]["probe"] == "ok"
    assert body["llm"]["latency_ms"] is not None


def test_health_probe_failure_degrades(client, monkeypatch):
    _seed()

    def boom(*args, **kwargs):
        raise ConnectionError("dns")

    monkeypatch.setattr("requests.post", boom)
    health._probe_cache["at"] = 0.0
    health._probe_cache["result"] = None
    body = client.get("/health?probe=1").json()
    assert body["llm"]["probe"] == "failed"
    assert body["llm"]["error"] == "ConnectionError"
    assert body["status"] == "degraded"


def test_health_probe_is_cached(client, monkeypatch):
    _seed()
    calls = {"n": 0}

    class FakeResponse:
        status_code = 200

    def counting(*args, **kwargs):
        calls["n"] += 1
        return FakeResponse()

    monkeypatch.setattr("requests.post", counting)
    health._probe_cache["at"] = 0.0
    health._probe_cache["result"] = None
    client.get("/health?probe=1")
    client.get("/health?probe=1")
    assert calls["n"] == 1


def test_health_probe_http_error(client, monkeypatch):
    _seed()

    class FakeResponse:
        status_code = 429

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse())
    health._probe_cache["at"] = 0.0
    health._probe_cache["result"] = None
    body = client.get("/health?probe=1").json()
    assert body["llm"]["probe"] == "failed"
    assert body["llm"]["error"] == "HTTP 429"


def test_health_reports_acl_state(client, monkeypatch):
    from src import acl

    monkeypatch.setattr(settings, "ACL_ENABLED", False)
    acl.refresh()
    body = client.get("/health").json()
    assert body["acl"] == {"enabled": False, "ok": True, "rules": 0, "error": None}


def test_health_is_down_when_acl_is_broken(client, tmp_path, monkeypatch):
    from src import acl

    _seed(count=2)
    monkeypatch.setattr(settings, "ACL_ENABLED", True)
    monkeypatch.setattr(settings, "ACL_FILE", str(tmp_path / "missing-acl.json"))
    acl.refresh()
    try:
        resp = client.get("/health")
        body = resp.json()
        # A broken ACL hides every document, so readiness must not say "ok".
        assert resp.status_code == 503
        assert body["status"] == "down"
        assert body["acl"]["enabled"] is True
        assert body["acl"]["ok"] is False
        assert "does not exist" in body["acl"]["error"]
    finally:
        monkeypatch.setattr(settings, "ACL_ENABLED", False)
        acl.refresh()


def test_startup_refuses_broken_acl(tmp_path, monkeypatch):
    """A deployment with ACL_ENABLED=true and no file must not start."""
    import importlib

    from src import acl

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "ACL_ENABLED", True)
    monkeypatch.setattr(settings, "ACL_FILE", str(tmp_path / "missing-acl.json"))
    (tmp_path / "data").mkdir()
    acl.refresh()
    import main

    importlib.reload(main)
    try:
        with pytest.raises(RuntimeError, match="document ACL misconfigured"):
            with TestClient(main.app):
                pass
    finally:
        monkeypatch.setattr(settings, "ACL_ENABLED", False)
        acl.refresh()
        importlib.reload(main)
