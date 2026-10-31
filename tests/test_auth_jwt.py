"""JWT authentication: token validation and identity precedence."""

import time

import pytest
from fastapi.testclient import TestClient

from src import auth, chat_service
from src.config import settings
from src.schemas import ChatRequest

SECRET = "test-secret-that-is-long-enough-123456"


@pytest.fixture(autouse=True)
def jwt_mode(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    monkeypatch.setattr(settings, "AUTH_JWT_ALGORITHM", "HS256")
    monkeypatch.setattr(settings, "AUTH_JWT_ISSUER", "")
    monkeypatch.setattr(settings, "AUTH_JWT_AUDIENCE", "")
    yield
    monkeypatch.setattr(settings, "AUTH_MODE", "none")


def _identity(**kwargs):
    defaults = dict(user_id="alice", departments=("Tech",), groups=(), is_admin=False)
    defaults.update(kwargs)
    return auth.Identity(**defaults)


def test_roundtrip():
    token = auth.encode_token(_identity(groups=("exec",)))
    identity = auth.decode_token(token)
    assert identity.user_id == "alice"
    assert identity.departments == ("Tech",)
    assert identity.groups == ("exec",)
    assert identity.is_admin is False


def test_expired_token_rejected():
    token = auth.encode_token(_identity(), ttl_seconds=-1)
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)


def test_wrong_secret_rejected(monkeypatch):
    token = auth.encode_token(_identity())
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", "another-secret-that-is-long-enough-xyz")
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)


def test_tampered_token_rejected():
    token = auth.encode_token(_identity())
    with pytest.raises(auth.AuthError):
        auth.decode_token(token[:-2] + ("aa" if token.endswith("bb") else "bb"))


def test_missing_subject_rejected(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    import jwt

    token = jwt.encode(
        {"exp": int(time.time()) + 60, "depts": ["Tech"]}, SECRET, algorithm="HS256"
    )
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)


def test_issuer_and_audience_enforced(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_JWT_ISSUER", "kb")
    monkeypatch.setattr(settings, "AUTH_JWT_AUDIENCE", "app")
    token = auth.encode_token(_identity())
    assert auth.decode_token(token).user_id == "alice"

    monkeypatch.setattr(settings, "AUTH_JWT_ISSUER", "other")
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)


def test_bearer_extraction():
    assert auth.bearer_token({"Authorization": "Bearer abc"}) == "abc"
    assert auth.bearer_token({"authorization": "bearer abc"}) == "abc"
    assert auth.bearer_token({"X-Auth-Token": "xyz"}) == "xyz"
    assert auth.bearer_token({}) == ""


def test_validate_config_requires_secret(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", "short")
    with pytest.raises(RuntimeError):
        auth.validate_config()
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    auth.validate_config()


def test_body_identity_is_ignored_in_jwt_mode(monkeypatch):
    token = auth.encode_token(_identity(departments=("Tech",)))
    request = ChatRequest(
        user_id="attacker", user_dept="Finance", message="hi"
    )
    prepared = chat_service.prepare(
        request, "chat", {"authorization": f"Bearer {token}"}
    )
    assert prepared.inputs["user_id"] == "alice"
    assert prepared.inputs["user_dept"] == "Tech"
    assert prepared.inputs["user_depts"] == ["Tech"]


def test_missing_token_raises_401(monkeypatch):
    from fastapi import HTTPException

    request = ChatRequest(user_id="u1", user_dept="Tech", message="hi")
    with pytest.raises(HTTPException) as exc:
        chat_service.prepare(request, "chat", {})
    assert exc.value.status_code == 401


def test_cache_scope_uses_token_departments(monkeypatch):
    token = auth.encode_token(_identity(departments=("HR",), groups=("hr",)))
    prepared = chat_service.prepare(
        ChatRequest(user_id="x", user_dept="Tech", message="hi"),
        "chat",
        {"authorization": f"Bearer {token}"},
    )
    assert prepared.scope.startswith("HR")


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


def _stub_agent(monkeypatch):
    from langchain_core.documents import Document
    from langchain_core.messages import AIMessage

    import src.agent as agent_module

    class Engine:
        @property
        def embeddings(self):
            raise AssertionError("not used")

        def search(self, *_a, **_k):
            return [
                Document(
                    page_content="policy text",
                    metadata={"source": "Tech/a.md", "dept": "Tech", "kind": "md",
                              "page": 1, "slide": 0, "sheet": "", "title": ""},
                )
            ]

    class Stub:
        def invoke(self, _messages):
            return AIMessage(content="ok")

    monkeypatch.setattr(agent_module, "get_engine", lambda: Engine())
    monkeypatch.setattr(agent_module, "llm", Stub())


def test_api_requires_token(client, monkeypatch):
    _stub_agent(monkeypatch)
    assert client.post("/chat", json={"user_id": "u", "message": "hi"}).status_code == 401


def test_api_accepts_valid_token(client, monkeypatch):
    _stub_agent(monkeypatch)
    token = auth.encode_token(_identity())
    resp = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text


def test_admin_accepts_admin_token(client):
    token = auth.encode_token(_identity(is_admin=True))
    resp = client.get("/admin/api/meta", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_admin_rejects_non_admin_token(client):
    token = auth.encode_token(_identity(is_admin=False))
    resp = client.get("/admin/api/meta", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code in (401, 403)


def test_static_admin_token_still_works_as_break_glass(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "break-glass")
    resp = client.get("/admin/api/meta", headers={"X-Admin-Token": "break-glass"})
    assert resp.status_code == 200
