"""CORS middleware and the cross-origin admin guard."""

import pytest
from fastapi.testclient import TestClient

from src.config import settings


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    import importlib

    def factory(cors=""):
        monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
        monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
        monkeypatch.setattr(settings, "CORS_ORIGINS", cors)
        (tmp_path / "data").mkdir(exist_ok=True)
        import main

        importlib.reload(main)
        return TestClient(main.app)

    yield factory
    monkeypatch.setattr(settings, "CORS_ORIGINS", "")


def test_no_cors_headers_by_default(make_client):
    client = make_client("")
    resp = client.get("/info", headers={"Origin": "https://app.example.com"})
    assert "access-control-allow-origin" not in resp.headers


def test_allowed_origin_gets_header(make_client):
    client = make_client("https://app.example.com")
    resp = client.get("/info", headers={"Origin": "https://app.example.com"})
    assert resp.headers["access-control-allow-origin"] == "https://app.example.com"


def test_unknown_origin_is_rejected(make_client):
    client = make_client("https://app.example.com")
    resp = client.get("/info", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in resp.headers


def test_preflight_allows_chat(make_client):
    client = make_client("https://app.example.com")
    resp = client.options(
        "/chat",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-api-key",
        },
    )
    assert resp.status_code in (200, 204)
    assert resp.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "x-api-key" in resp.headers["access-control-allow-headers"].lower()


def test_exposes_request_id(make_client):
    client = make_client("https://app.example.com")
    resp = client.get("/info", headers={"Origin": "https://app.example.com"})
    assert "x-request-id" in resp.headers.get("access-control-expose-headers", "").lower()


def test_admin_blocks_foreign_origin(make_client, tmp_path):
    client = make_client("https://app.example.com")
    (tmp_path / "data" / "Tech").mkdir(exist_ok=True)
    resp = client.get(
        "/admin/api/meta", headers={"Origin": "https://evil.example.com"}
    )
    assert resp.status_code == 403
    assert "cross-origin" in resp.text


def test_admin_allows_same_origin(make_client):
    client = make_client("")
    resp = client.get(
        "/admin/api/meta", headers={"Origin": "http://testserver", "Host": "testserver"}
    )
    assert resp.status_code == 200


def test_admin_allows_configured_origin(make_client):
    client = make_client("https://app.example.com")
    resp = client.get(
        "/admin/api/meta", headers={"Origin": "https://app.example.com"}
    )
    assert resp.status_code == 200
