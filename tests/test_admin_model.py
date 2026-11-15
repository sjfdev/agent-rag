"""Admin console endpoints for the chat model overrides.

管理台对话模型覆盖接口测试。
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from src import model_settings
from src.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(settings, "JOBS_PATH", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(settings, "MODEL_SETTINGS_FILE", str(tmp_path / "model_settings.json"))
    monkeypatch.setattr(settings, "LLM_API_BASE", "https://env.example/v1")
    monkeypatch.setattr(settings, "LLM_API_KEY", "env-secret-key-1234")
    monkeypatch.setattr(settings, "LLM_MODEL", "env-model")
    monkeypatch.setattr(settings, "LLM_TEMPERATURE", 0.0)
    monkeypatch.setattr(settings, "LLM_MAX_TOKENS", 256)
    model_settings.reset_state()
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client, tmp_path
    model_settings.reset_state()


def _payload(**overrides):
    data = {
        "api_base": "https://new.example/v1",
        "model": "new-model",
        "api_key": "sk-test-abcdefgh",
        "temperature": 0.3,
        "max_tokens": 512,
    }
    data.update(overrides)
    return data


def test_get_model_shows_env_defaults(client):
    test_client, _ = client
    body = test_client.get("/admin/api/model").json()
    assert body["source"] == "env"
    assert body["model"] == "env-model"
    assert body["has_api_key"] is True
    assert body["api_key_hint"] == "****1234"
    assert "env-secret-key-1234" not in test_client.get("/admin/api/model").text


def test_put_model_persists_and_is_used_immediately(client):
    test_client, tmp_path = client
    resp = test_client.put("/admin/api/model", json=_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "override"
    assert body["model"] == "new-model"
    assert body["api_key_hint"] == "****efgh"

    assert (tmp_path / "model_settings.json").exists()
    assert test_client.get("/info").json()["llm_model"] == "new-model"
    assert test_client.get("/admin/api/meta").json()["llm_model"] == "new-model"

    from src import agent

    bound = agent.current_llm()
    assert bound.bound.model_name == "new-model"


def test_put_model_rejects_invalid_payload(client):
    test_client, tmp_path = client
    resp = test_client.put("/admin/api/model", json=_payload(api_base="ftp://bad"))
    assert resp.status_code == 400
    assert "http" in resp.json()["detail"]
    assert not (tmp_path / "model_settings.json").exists()


def test_delete_model_restores_env(client):
    test_client, tmp_path = client
    test_client.put("/admin/api/model", json=_payload())
    resp = test_client.delete("/admin/api/model")
    assert resp.status_code == 200
    assert resp.json()["source"] == "env"
    assert not (tmp_path / "model_settings.json").exists()
    assert test_client.get("/info").json()["llm_model"] == "env-model"


def test_put_model_is_audited_without_the_key(client):
    test_client, tmp_path = client
    test_client.put("/admin/api/model", json=_payload())
    records = test_client.get("/admin/api/audit").json()["records"]
    entry = next(r for r in records if r["action"] == "model.update")
    assert entry["detail"]["model"] == "new-model"
    assert "sk-test-abcdefgh" not in str(entry)


def test_model_test_endpoint_reports_failure(client, monkeypatch):
    test_client, _ = client

    def boom(payload=None):
        return {"ok": False, "model": "new-model", "error": "connection refused", "latency_ms": 3}

    monkeypatch.setattr(model_settings, "probe", boom)
    resp = test_client.post("/admin/api/model/test", json=_payload())
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["error"] == "connection refused"


def test_model_test_endpoint_validates_payload(client):
    test_client, _ = client
    resp = test_client.post("/admin/api/model/test", json=_payload(api_base="ftp://bad"))
    assert resp.status_code == 400


def test_model_test_with_empty_body_uses_the_current_config(client, monkeypatch):
    test_client, _ = client
    seen = {}

    def fake(payload=None):
        seen["payload"] = payload
        return {"ok": True, "model": "env-model", "latency_ms": 1}

    monkeypatch.setattr(model_settings, "probe", fake)
    resp = test_client.post("/admin/api/model/test", json={})
    assert resp.status_code == 200
    assert seen["payload"] is None
