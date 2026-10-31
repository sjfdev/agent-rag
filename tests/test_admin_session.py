"""Admin session endpoint: how the console learns who it is."""

import pytest
from fastapi.testclient import TestClient

from src import auth
from src.config import settings

SECRET = "test-secret-that-is-long-enough-123456"


def _identity(**kwargs):
    defaults = dict(user_id="alice", departments=("Tech",), groups=(), is_admin=True)
    defaults.update(kwargs)
    return auth.Identity(**defaults)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    (tmp_path / "data").mkdir()
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client


def test_session_in_none_mode(client):
    body = client.get("/admin/api/session").json()
    assert body["auth_mode"] == "none"
    assert body["authenticated"] is True
    assert body["is_admin"] is True


def test_session_requires_token_in_jwt_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    try:
        resp = client.get("/admin/api/session")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
    finally:
        monkeypatch.setattr(settings, "AUTH_MODE", "none")


def test_session_reports_admin_claims(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    try:
        token = auth.encode_token(_identity(groups=("exec",)))
        body = client.get(
            "/admin/api/session", headers={"Authorization": f"Bearer {token}"}
        ).json()
        assert body["auth_mode"] == "jwt"
        assert body["user_id"] == "alice"
        assert body["depts"] == ["Tech"]
        assert body["groups"] == ["exec"]
        assert body["is_admin"] is True
        assert body["source"] == "token"
    finally:
        monkeypatch.setattr(settings, "AUTH_MODE", "none")


def test_session_reports_non_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    try:
        token = auth.encode_token(_identity(is_admin=False))
        body = client.get(
            "/admin/api/session", headers={"Authorization": f"Bearer {token}"}
        ).json()
        assert body["is_admin"] is False
        # The session endpoint answers 200 so the UI can explain the 403.
        assert body["user_id"] == "alice"
    finally:
        monkeypatch.setattr(settings, "AUTH_MODE", "none")


def test_session_accepts_break_glass_token(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "break-glass")
    try:
        body = client.get(
            "/admin/api/session", headers={"X-Admin-Token": "break-glass"}
        ).json()
        assert body["source"] == "admin_token"
        assert body["is_admin"] is True
    finally:
        monkeypatch.setattr(settings, "AUTH_MODE", "none")
        monkeypatch.setattr(settings, "ADMIN_TOKEN", "")


def test_session_rejects_bad_token(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    try:
        resp = client.get(
            "/admin/api/session", headers={"Authorization": "Bearer not-a-jwt"}
        )
        assert resp.status_code == 401
    finally:
        monkeypatch.setattr(settings, "AUTH_MODE", "none")


def test_admin_audit_actor_is_the_token_subject(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    try:
        token = auth.encode_token(_identity())
        client.post(
            "/admin/api/dept",
            json={"name": "JwtDept"},
            headers={"Authorization": f"Bearer {token}"},
        )
        log = (tmp_path / "audit.log").read_text()
        assert "user:alice" in log
    finally:
        monkeypatch.setattr(settings, "AUTH_MODE", "none")
