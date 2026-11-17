"""Admin API for runtime chunking overrides.

管理台分片配置接口测试。
"""

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from src import chunk_settings
from src.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CHUNK_SETTINGS_FILE", str(tmp_path / "chunk_settings.json"))
    monkeypatch.setattr(settings, "CHUNK_TOKENS", 450)
    monkeypatch.setattr(settings, "CHUNK_OVERLAP_TOKENS", 50)
    monkeypatch.setattr(settings, "CHUNK_CHINESE_PUNCTUATION", True)
    monkeypatch.setattr(settings, "CHUNK_TITLE_PREFIX", True)
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    chunk_settings.reset_state()
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client, tmp_path
    chunk_settings.reset_state()


def _payload(**overrides):
    data = {
        "chunk_tokens": 420,
        "chunk_overlap_tokens": 40,
        "chinese_punctuation": False,
        "title_prefix": False,
    }
    data.update(overrides)
    return data


def test_get_chunking_shows_env_defaults(client):
    test_client, _ = client
    body = test_client.get("/admin/api/chunking").json()
    assert body["source"] == "env"
    assert body["config"]["chunk_tokens"] == 450
    assert body["config"]["chunk_overlap_tokens"] == 50
    assert body["signature"]
    assert body["tokenizer"].startswith("heuristic:")


def test_put_chunking_persists_and_changes_signature(client):
    test_client, tmp_path = client
    before = test_client.get("/admin/api/chunking").json()
    resp = test_client.put("/admin/api/chunking", json=_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "override"
    assert body["config"] == _payload()
    assert body["signature"] != before["signature"]

    path = tmp_path / "chunk_settings.json"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["chunk_tokens"] == 420
    assert test_client.get("/admin/api/chunking").json()["config"] == _payload()


@pytest.mark.parametrize(
    "payload",
    [
        _payload(chunk_tokens=10),
        _payload(chunk_tokens=99999),
        _payload(chunk_overlap_tokens=300, chunk_tokens=450),
        _payload(chunk_overlap_tokens=500, chunk_tokens=450),
    ],
)
def test_put_chunking_rejects_invalid_payload(client, payload):
    test_client, tmp_path = client
    resp = test_client.put("/admin/api/chunking", json=payload)
    assert resp.status_code == 400
    assert not (tmp_path / "chunk_settings.json").exists()


def test_delete_chunking_restores_env(client):
    test_client, tmp_path = client
    test_client.put("/admin/api/chunking", json=_payload())
    resp = test_client.delete("/admin/api/chunking")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "env"
    assert body["config"]["chunk_tokens"] == 450
    assert not (tmp_path / "chunk_settings.json").exists()


def test_chunking_changes_are_audited(client):
    test_client, _ = client
    test_client.put("/admin/api/chunking", json=_payload())
    test_client.delete("/admin/api/chunking")
    records = test_client.get("/admin/api/audit").json()["records"]
    actions = {record["action"] for record in records}
    assert "chunk.update" in actions
    assert "chunk.reset" in actions
    entry = next(record for record in records if record["action"] == "chunk.update")
    assert entry["detail"]["config"]["chunk_tokens"] == 420
