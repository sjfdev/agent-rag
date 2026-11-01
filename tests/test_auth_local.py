"""Local username/password accounts (no IdP): hashing, store, login, revocation."""

import json

import pytest
from fastapi.testclient import TestClient

from src import auth, local_auth
from src.config import settings

SECRET = "test-secret-that-is-long-enough-123456"
PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    """Keep the suite fast: real scrypt would cost ~150 ms per hash.

    The parameters are read from the module, and `verify_password` reads them
    back from the encoded string, so a smaller factor exercises the same code
    path (including the tampered-parameters guard).
    """
    monkeypatch.setattr(local_auth, "SCRYPT_N", 2 ** 8)
    yield


@pytest.fixture()
def users_file(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    monkeypatch.setattr(settings, "AUTH_USERS_FILE", str(path))
    monkeypatch.setattr(settings, "AUTH_LOCAL_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    monkeypatch.setattr(settings, "AUTH_JWKS_FILE", "")
    monkeypatch.setattr(settings, "AUTH_JWKS_URL", "")
    local_auth.clear_cache()
    local_auth.reset_login_limiter()
    yield path
    local_auth.clear_cache()
    local_auth.reset_login_limiter()
    monkeypatch.setattr(settings, "AUTH_MODE", "none")
    monkeypatch.setattr(settings, "AUTH_LOCAL_LOGIN_ENABLED", False)


@pytest.fixture()
def client(users_file, tmp_path, monkeypatch):
    import importlib

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    (tmp_path / "data").mkdir()
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client


# --- password hashing ------------------------------------------------------


def test_hash_is_self_describing_and_verifies():
    encoded = local_auth.hash_password(PASSWORD)
    assert encoded.startswith("scrypt$")
    assert PASSWORD not in encoded
    assert local_auth.verify_password(PASSWORD, encoded) is True
    assert local_auth.verify_password("wrong-password-x", encoded) is False


def test_hashing_is_salted():
    assert local_auth.hash_password(PASSWORD) != local_auth.hash_password(PASSWORD)


def test_short_password_rejected():
    with pytest.raises(local_auth.LocalAuthError):
        local_auth.hash_password("short")


def test_verify_rejects_garbage():
    assert local_auth.verify_password(PASSWORD, "") is False
    assert local_auth.verify_password(PASSWORD, "not-a-hash") is False
    assert local_auth.verify_password("", local_auth.hash_password(PASSWORD)) is False


def test_verify_rejects_absurd_parameters():
    encoded = local_auth.hash_password(PASSWORD)
    scheme, _n, r, p, salt, digest = encoded.split("$")
    # A tampered file must not turn one login into a multi-second CPU stall.
    huge = "$".join([scheme, str(2 ** 30), r, p, salt, digest])
    assert local_auth.verify_password(PASSWORD, huge) is False


# --- user store ------------------------------------------------------------


def test_create_and_read_user(users_file):
    created = local_auth.create_user(
        "alice", PASSWORD, depts=["Tech", "HR"], groups=["exec"], admin=True
    )
    assert created["username"] == "alice"
    assert created["depts"] == ["Tech", "HR"]
    assert created["admin"] is True
    assert created["token_version"] == 1
    assert "password" not in created
    assert local_auth.get_user("alice")["password"].startswith("scrypt$")


def test_users_file_is_private_and_atomic(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    assert users_file.is_file()
    assert not users_file.with_name(users_file.name + ".tmp").exists()
    assert oct(users_file.stat().st_mode)[-3:] == "600"
    payload = json.loads(users_file.read_text())
    assert payload["version"] == 1
    assert "alice" in payload["users"]


def test_duplicate_user_rejected(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    with pytest.raises(local_auth.LocalAuthError):
        local_auth.create_user("alice", PASSWORD, depts=["Tech"])


@pytest.mark.parametrize("name", ["", "  ", "al ice", "bob;drop", "a" * 65, "bob/../x"])
def test_invalid_usernames_rejected(users_file, name):
    with pytest.raises(local_auth.LocalAuthError):
        local_auth.create_user(name, PASSWORD, depts=["Tech"])


def test_invalid_department_rejected(users_file):
    with pytest.raises(local_auth.LocalAuthError):
        local_auth.create_user("alice", PASSWORD, depts=["bad dept"])


def test_missing_file_reads_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_USERS_FILE", str(tmp_path / "nope.json"))
    local_auth.clear_cache()
    assert local_auth.list_users() == []
    assert local_auth.user_count() == 0


def test_corrupt_file_does_not_crash(users_file):
    users_file.write_text("{not json")
    local_auth.clear_cache()
    assert local_auth.list_users() == []


def test_external_edit_is_picked_up(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    payload = json.loads(users_file.read_text())
    payload["users"]["bob"] = dict(payload["users"]["alice"])
    users_file.write_text(json.dumps(payload))
    # mtime-based cache: a different file must be re-read without a restart.
    assert {u["username"] for u in local_auth.list_users()} == {"alice", "bob"}


def test_delete_user(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    assert local_auth.delete_user("alice") is True
    assert local_auth.delete_user("alice") is False
    assert local_auth.list_users() == []


# --- authentication --------------------------------------------------------


def test_authenticate(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"], admin=True)
    record, reason = local_auth.authenticate("alice", PASSWORD)
    assert record["username"] == "alice"
    assert reason == ""
    assert local_auth.authenticate("alice", "nope-nope-nope") == (None, "bad password")
    assert local_auth.authenticate("ghost", PASSWORD) == (None, "unknown user")
    assert local_auth.authenticate("", "") == (None, "missing credentials")


def test_authenticate_rejects_disabled(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    local_auth.update_user("alice", disabled=True)
    assert local_auth.authenticate("alice", PASSWORD) == (None, "account disabled")


def test_password_change_bumps_token_version(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    assert local_auth.token_version_of("alice") == 1
    local_auth.set_password("alice", "a-brand-new-password")
    assert local_auth.token_version_of("alice") == 2
    assert local_auth.authenticate("alice", PASSWORD)[0] is None
    assert local_auth.authenticate("alice", "a-brand-new-password")[0] is not None


def test_permission_change_bumps_token_version(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    local_auth.update_user("alice", depts=["Finance"])
    assert local_auth.token_version_of("alice") == 2


def test_noop_update_keeps_token_version(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    local_auth.update_user("alice", depts=["Tech"])
    assert local_auth.token_version_of("alice") == 1


def test_admin_count_ignores_disabled(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"], admin=True)
    local_auth.create_user("bob", PASSWORD, depts=["Tech"], admin=True)
    assert local_auth.admin_count() == 2
    local_auth.update_user("alice", disabled=True)
    assert local_auth.admin_count() == 1


# --- token binding ---------------------------------------------------------


def test_local_token_carries_version_and_validates(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"], admin=True)
    record, _ = local_auth.authenticate("alice", PASSWORD)
    identity = auth.Identity(
        user_id="alice",
        departments=tuple(record["depts"]),
        is_admin=bool(record["admin"]),
        token_version=record["token_version"],
    )
    token = auth.encode_token(identity)
    assert auth.decode_token(token).token_version == 1
    # identity_from_headers applies the revocation check.
    resolved = auth.identity_from_headers({"authorization": f"Bearer {token}"})
    assert resolved.user_id == "alice"


def test_disabling_an_account_revokes_its_token(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    identity = auth.Identity("alice", ("Tech",), (), False, token_version=1)
    token = auth.encode_token(identity)
    local_auth.update_user("alice", disabled=True)
    with pytest.raises(auth.AuthError):
        auth.identity_from_headers({"authorization": f"Bearer {token}"})


def test_deleting_an_account_revokes_its_token(users_file):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    identity = auth.Identity("alice", ("Tech",), (), False, token_version=1)
    token = auth.encode_token(identity)
    local_auth.delete_user("alice")
    with pytest.raises(auth.AuthError):
        auth.identity_from_headers({"authorization": f"Bearer {token}"})


def test_external_tokens_are_not_bound_to_the_users_file(users_file):
    # Gateway/IdP tokens carry no `ver` claim: the local store must not
    # invalidate them (otherwise enabling local login would break IdP setups).
    token = auth.encode_token(auth.Identity("gateway-user", ("Tech",), (), True))
    resolved = auth.identity_from_headers({"authorization": f"Bearer {token}"})
    assert resolved.user_id == "gateway-user"
    assert resolved.token_version is None


def test_invalid_version_claim_rejected(users_file):
    import jwt

    token = jwt.encode(
        {"sub": "alice", "exp": 9999999999, "ver": "not-a-number"},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)


# --- HTTP endpoints --------------------------------------------------------


def test_login_returns_a_usable_token(client):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"], admin=True)
    resp = client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["user"]["username"] == "alice"

    session = client.get(
        "/admin/api/session", headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert session.status_code == 200
    assert session.json()["user_id"] == "alice"
    assert session.json()["is_admin"] is True


def test_login_accepts_form_encoded(client):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    resp = client.post("/auth/login", data={"username": "alice", "password": PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["token"]


def test_login_rejects_bad_credentials(client):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    bad = client.post("/auth/login", json={"username": "alice", "password": "wrong-one-here"})
    assert bad.status_code == 401
    assert bad.json()["detail"] == "invalid username or password"
    unknown = client.post("/auth/login", json={"username": "ghost", "password": "whatever-x"})
    # Identical message and status: the endpoint must not enumerate users.
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == bad.json()["detail"]


def test_login_rejects_disabled_account(client):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    local_auth.update_user("alice", disabled=True)
    resp = client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert resp.status_code == 401


def test_login_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_LOGIN_RATE_LIMIT", 2)
    local_auth.reset_login_limiter()
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    codes = [
        client.post("/auth/login", json={"username": "alice", "password": "wrong-one-here"}).status_code
        for _ in range(3)
    ]
    assert codes[:2] == [401, 401]
    assert codes[2] == 429
    limited = client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()
    monkeypatch.setattr(settings, "AUTH_LOGIN_RATE_LIMIT", 10)
    local_auth.reset_login_limiter()


def test_login_is_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_LOCAL_LOGIN_ENABLED", False)
    resp = client.post("/auth/login", json={"username": "a", "password": "b"})
    assert resp.status_code == 404


def test_login_writes_audit_records(client, tmp_path):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    client.post("/auth/login", json={"username": "alice", "password": "wrong-one-here"})
    log = (tmp_path / "audit.log").read_text()
    records = [json.loads(line) for line in log.splitlines() if line.strip()]
    logins = [r for r in records if r["action"] == "auth.login"]
    assert any(r["ok"] is True and r["actor"] == "user:alice" for r in logins)
    assert any(r["ok"] is False and "anonymous:" in r["actor"] for r in logins)


def test_info_reports_local_login(client):
    body = client.get("/info").json()
    assert body["local_login"] is True


def test_token_stops_working_after_password_change(client):
    local_auth.create_user("alice", PASSWORD, depts=["Tech"], admin=True)
    token = client.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/admin/api/session", headers=headers).status_code == 200
    local_auth.set_password("alice", "a-brand-new-password")
    assert client.get("/admin/api/session", headers=headers).status_code == 401


def test_chat_accepts_a_local_token(client):
    from src import chat_service

    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    token = client.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json()["token"]
    prepared = chat_service.prepare(
        __import__("src.schemas", fromlist=["ChatRequest"]).ChatRequest(
            user_dept="Finance", message="hi"
        ),
        "chat",
        {"authorization": f"Bearer {token}"},
    )
    # The token wins over the body: an Alice token cannot ask for Finance.
    assert prepared.inputs["user_dept"] == "Tech"


# --- admin API -------------------------------------------------------------


@pytest.fixture()
def admin_headers(client):
    local_auth.create_user("root", PASSWORD, depts=["Tech"], admin=True)
    token = client.post(
        "/auth/login", json={"username": "root", "password": PASSWORD}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_admin_lists_users(client, admin_headers):
    resp = client.get("/admin/api/users", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [u["username"] for u in body["users"]] == ["root"]
    assert body["enabled"] is True
    assert "password" not in json.dumps(body)


def test_admin_creates_and_deletes_user(client, admin_headers):
    created = client.post(
        "/admin/api/users",
        headers=admin_headers,
        json={"username": "bob", "password": "bob-password-1", "depts": ["HR"]},
    )
    assert created.status_code == 200
    assert created.json()["username"] == "bob"
    # The new account can log in immediately.
    assert client.post(
        "/auth/login", json={"username": "bob", "password": "bob-password-1"}
    ).status_code == 200

    deleted = client.delete("/admin/api/users/bob", headers=admin_headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.post(
        "/auth/login", json={"username": "bob", "password": "bob-password-1"}
    ).status_code == 401


def test_admin_create_rejects_duplicate(client, admin_headers):
    resp = client.post(
        "/admin/api/users",
        headers=admin_headers,
        json={"username": "root", "password": "another-password-1", "depts": ["HR"]},
    )
    assert resp.status_code == 400


def test_admin_updates_permissions(client, admin_headers):
    client.post(
        "/admin/api/users",
        headers=admin_headers,
        json={"username": "bob", "password": "bob-password-1", "depts": ["HR"]},
    )
    resp = client.patch(
        "/admin/api/users/bob",
        headers=admin_headers,
        json={"depts": ["Finance"], "groups": ["exec"], "admin": True},
    )
    assert resp.status_code == 200
    assert resp.json()["depts"] == ["Finance"]
    assert resp.json()["groups"] == ["exec"]
    assert resp.json()["admin"] is True


def test_admin_can_reset_a_password(client, admin_headers):
    client.post(
        "/admin/api/users",
        headers=admin_headers,
        json={"username": "bob", "password": "bob-password-1", "depts": ["HR"]},
    )
    resp = client.patch(
        "/admin/api/users/bob", headers=admin_headers, json={"password": "bob-password-2"}
    )
    assert resp.status_code == 200
    assert client.post(
        "/auth/login", json={"username": "bob", "password": "bob-password-1"}
    ).status_code == 401
    assert client.post(
        "/auth/login", json={"username": "bob", "password": "bob-password-2"}
    ).status_code == 200


def test_admin_cannot_lock_itself_out(client, admin_headers):
    # root is the only enabled admin: demoting it must be refused.
    demote = client.patch(
        "/admin/api/users/root", headers=admin_headers, json={"admin": False}
    )
    assert demote.status_code == 409
    disable = client.patch(
        "/admin/api/users/root", headers=admin_headers, json={"disabled": True}
    )
    assert disable.status_code == 409
    delete = client.delete("/admin/api/users/root", headers=admin_headers)
    assert delete.status_code == 409
    # ... and root still works.
    assert client.get("/admin/api/users", headers=admin_headers).status_code == 200


def test_admin_can_demote_when_another_admin_exists(client, admin_headers):
    client.post(
        "/admin/api/users",
        headers=admin_headers,
        json={"username": "bob", "password": "bob-password-1", "depts": ["HR"], "admin": True},
    )
    resp = client.patch(
        "/admin/api/users/root", headers=admin_headers, json={"admin": False}
    )
    assert resp.status_code == 200


def test_admin_user_routes_are_404_without_local_login(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_LOCAL_LOGIN_ENABLED", False)
    assert client.get("/admin/api/users", headers=admin_headers).status_code == 404


def test_admin_user_routes_require_admin(client):
    local_auth.create_user("bob", PASSWORD, depts=["HR"])
    token = client.post(
        "/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["token"]
    resp = client.get("/admin/api/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_admin_user_audit_records(client, admin_headers, tmp_path):
    client.post(
        "/admin/api/users",
        headers=admin_headers,
        json={"username": "bob", "password": "bob-password-1", "depts": ["HR"]},
    )
    client.patch("/admin/api/users/bob", headers=admin_headers, json={"admin": False})
    client.delete("/admin/api/users/bob", headers=admin_headers)
    log = (tmp_path / "audit.log").read_text()
    actions = [json.loads(line)["action"] for line in log.splitlines() if line.strip()]
    assert "user.create" in actions
    assert "user.update" in actions
    assert "user.delete" in actions


# --- startup ---------------------------------------------------------------


def test_local_login_requires_jwt_mode(users_file, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "none")
    assert local_auth.enabled() is False
    assert local_auth.stats()["enabled"] is False


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_LOCAL_LOGIN_ENABLED", False)
    assert local_auth.enabled() is False


def test_login_rate_limit_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_LOGIN_RATE_LIMIT", 0)
    local_auth.reset_login_limiter()
    for _ in range(5):
        resp = client.post(
            "/auth/login", json={"username": "ghost", "password": "whatever-x"}
        )
        assert resp.status_code == 401
    local_auth.reset_login_limiter()


def test_local_login_coexists_with_oidc_routes(client, monkeypatch):
    """GET /auth/login (IdP redirect) and POST /auth/login (accounts) differ by method."""
    local_auth.create_user("alice", PASSWORD, depts=["Tech"])
    # OIDC is not configured in this fixture, so the GET answers 404 ...
    assert client.get("/auth/login", follow_redirects=False).status_code == 404
    # ... while the POST still logs in.
    assert client.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}
    ).status_code == 200

    monkeypatch.setattr(settings, "AUTH_OIDC_CLIENT_ID", "agent-rag")
    monkeypatch.setattr(settings, "AUTH_OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setattr(settings, "AUTH_OIDC_AUTHORIZE_URL", "https://idp.example.com/auth")
    monkeypatch.setattr(settings, "AUTH_OIDC_TOKEN_URL", "https://idp.example.com/token")
    monkeypatch.setattr(
        settings, "AUTH_OIDC_REDIRECT_URI", "https://kb.example.com/auth/callback"
    )
    try:
        assert client.get("/auth/login", follow_redirects=False).status_code in (302, 307)
        assert client.post(
            "/auth/login", json={"username": "alice", "password": PASSWORD}
        ).status_code == 200
    finally:
        for field in (
            "AUTH_OIDC_CLIENT_ID",
            "AUTH_OIDC_CLIENT_SECRET",
            "AUTH_OIDC_AUTHORIZE_URL",
            "AUTH_OIDC_TOKEN_URL",
            "AUTH_OIDC_REDIRECT_URI",
        ):
            monkeypatch.setattr(settings, field, "")


def test_invalid_update_does_not_rotate_the_password(client, admin_headers):
    """A rejected department must not leave the password already changed."""
    client.post(
        "/admin/api/users",
        headers=admin_headers,
        json={"username": "bob", "password": "bob-password-1", "depts": ["HR"]},
    )
    resp = client.patch(
        "/admin/api/users/bob",
        headers=admin_headers,
        json={"password": "bob-password-2", "depts": ["bad dept"]},
    )
    assert resp.status_code == 400
    # The old password still works...
    assert client.post(
        "/auth/login", json={"username": "bob", "password": "bob-password-1"}
    ).status_code == 200
    # ... and the new one was never applied.
    assert client.post(
        "/auth/login", json={"username": "bob", "password": "bob-password-2"}
    ).status_code == 401
