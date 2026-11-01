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


def test_health_reports_a_stalled_store(client, monkeypatch):
    """live_count raising must mark /health down, not silently return ok."""
    from src.engine import kb_engine

    def boom():
        raise RuntimeError("stalled")

    monkeypatch.setattr(kb_engine, "live_count", boom)
    resp = client.get("/health")
    body = resp.json()
    assert resp.status_code == 503
    assert body["status"] == "down"
    assert body["vector_store"]["ok"] is False
    assert body["vector_store"]["error"] == "RuntimeError"


def test_health_reports_storage(client):
    _seed(count=1)
    body = client.get("/health").json()
    storage = body["storage"]
    assert storage["ok"] is True
    assert storage["min_free_mb"] == settings.HEALTH_MIN_FREE_MB
    assert {p["name"] for p in storage["paths"]} == {"data", "index", "jobs", "audit"}
    assert body["status"] in {"ok", "degraded"}


def test_low_disk_degrades_health(client, monkeypatch):
    """A full disk breaks writes, not queries, so /health must say degraded."""

    class _Usage:
        free = 10 * 1024 * 1024  # 10 MB, below the 512 MB floor

    monkeypatch.setattr(health.shutil, "disk_usage", lambda _path: _Usage)
    _seed(count=1)
    resp = client.get("/health")
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "degraded"
    assert body["storage"]["ok"] is False
    assert body["storage"]["free_mb"] == 10.0


def test_storage_picks_the_tightest_directory(client, monkeypatch):
    sizes = {"data": 900, "db": 20, "logs": 400}

    def fake_free(path):
        for name, value in sizes.items():
            if name in str(path):
                return float(value)
        return 1000.0

    monkeypatch.setattr(health, "_free_mb", fake_free)
    report = health.storage_report()
    assert report["free_mb"] == 20.0
    assert "db" in report["path"]


def test_storage_threshold_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "HEALTH_MIN_FREE_MB", 0)
    monkeypatch.setattr(health, "_free_mb", lambda _path: 1.0)
    assert health.storage_report()["ok"] is True


def test_storage_tolerates_unreadable_paths(client, monkeypatch):
    monkeypatch.setattr(health, "_free_mb", lambda _path: None)
    report = health.storage_report()
    assert report["ok"] is True
    assert report["free_mb"] is None
