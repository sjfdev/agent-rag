"""OIDC browser login: /auth/login, /auth/callback and the state guard."""

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from src import auth
from src.config import settings

SECRET = "test-secret-that-is-long-enough-123456"
CLIENT_SECRET = "oidc-client-secret"
STATE_COOKIE = "agent_rag_oidc_state"

LOGIN_SETTINGS = {
    "AUTH_OIDC_CLIENT_ID": "agent-rag",
    "AUTH_OIDC_CLIENT_SECRET": CLIENT_SECRET,
    "AUTH_OIDC_AUTHORIZE_URL": "https://idp.example.com/auth",
    "AUTH_OIDC_TOKEN_URL": "https://idp.example.com/token",
    "AUTH_OIDC_REDIRECT_URI": "https://kb.example.com/auth/callback",
    "AUTH_OIDC_SCOPE": "openid profile",
}


def _sign_state(raw: str) -> str:
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    digest = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def _decode_state(state: str) -> str:
    payload = state.rsplit(".", 1)[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()


def _callback(client, raw: str, *, code: str = "abc", state: str = ""):
    """Call the callback with a matching state cookie, as a real browser would."""
    signed = state or _sign_state(raw)
    client.cookies.set(STATE_COOKIE, signed, path="/auth")
    try:
        return client.get(f"/auth/callback?code={code}&state={signed}")
    finally:
        client.cookies.delete(STATE_COOKIE, path="/auth")


def _login_state(client, next_path: str = "/admin") -> str:
    resp = client.get(f"/auth/login?next={next_path}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    return parse_qs(urlparse(resp.headers["location"]).query)["state"][0]


def _identity(**kwargs):
    defaults = dict(user_id="alice", departments=("Tech",), groups=(), is_admin=True)
    defaults.update(kwargs)
    return auth.Identity(**defaults)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    monkeypatch.setattr(settings, "AUTH_JWKS_FILE", "")
    monkeypatch.setattr(settings, "AUTH_JWKS_URL", "")
    for field, value in LOGIN_SETTINGS.items():
        monkeypatch.setattr(settings, field, value)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    (tmp_path / "data").mkdir()
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client
    monkeypatch.setattr(settings, "AUTH_MODE", "none")
    for field in LOGIN_SETTINGS:
        monkeypatch.setattr(settings, field, "")


def test_login_is_404_without_oidc_config(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setattr(settings, "AUTH_MODE", "none")
    for field in LOGIN_SETTINGS:
        monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    (tmp_path / "d").mkdir()
    import main

    importlib.reload(main)
    with TestClient(main.app) as c:
        assert c.get("/auth/login", follow_redirects=False).status_code == 404
        assert c.get("/auth/callback?code=x&state=y", follow_redirects=False).status_code == 404


def test_login_redirects_to_provider_with_signed_state(client):
    resp = client.get("/auth/login?next=/admin", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    parsed = urlparse(location)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://idp.example.com/auth"
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["agent-rag"]
    assert query["redirect_uri"] == ["https://kb.example.com/auth/callback"]
    assert query["scope"] == ["openid profile"]
    state = query["state"][0]
    payload, digest = state.rsplit(".", 1)
    expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(digest, expected)
    assert _decode_state(state).split("|", 1)[1] == "/admin"


def test_login_binds_state_to_a_cookie(client):
    resp = client.get("/auth/login?next=/admin", follow_redirects=False)
    cookie = resp.headers["set-cookie"].lower()
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    assert STATE_COOKIE in cookie
    assert state.lower() in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/auth" in cookie


def test_login_cookie_is_secure_behind_a_tls_proxy(client):
    resp = client.get(
        "/auth/login?next=/admin",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert "secure" in resp.headers["set-cookie"].lower()


def test_info_advertises_oidc_login(client):
    body = client.get("/info").json()
    assert body["auth_mode"] == "jwt"
    assert body["oidc_login"] is True


def test_callback_accepts_the_cookie_set_by_login(client, monkeypatch):
    """End-to-end: the state cookie from /auth/login is what unlocks the callback."""
    monkeypatch.setattr(
        auth,
        "exchange_code",
        lambda _code: {"identity": _identity(), "id_token": "provider", "raw": {}},
    )
    state = _login_state(client)  # TestClient keeps the Set-Cookie for us
    resp = client.get(f"/auth/callback?code=abc&state={state}")
    assert resp.status_code == 200
    assert "#token=" in resp.text


def test_callback_rejects_state_from_a_different_login(client, monkeypatch):
    """A state signed by us but issued to another browser session is rejected."""
    monkeypatch.setattr(
        auth,
        "exchange_code",
        lambda _code: {"identity": _identity(), "id_token": "provider", "raw": {}},
    )
    _login_state(client)
    other = _sign_state("other-nonce|/admin")
    resp = client.get(f"/auth/callback?code=abc&state={other}")
    assert resp.status_code == 400


def test_callback_requires_the_state_cookie(client):
    state = _sign_state("nonce|/admin")
    # No cookie: a valid signature alone must not be enough (login CSRF).
    resp = client.get(f"/auth/callback?code=abc&state={state}")
    assert resp.status_code == 400


def test_callback_rejects_tampered_state(client):
    state = _sign_state("nonce|/admin")
    tampered = state[:-1] + ("0" if state.endswith("1") else "1")
    resp = _callback(client, "nonce|/admin", state=tampered)
    assert resp.status_code == 400


def test_callback_rejects_missing_state(client):
    assert client.get("/auth/callback?code=abc").status_code == 400


def test_callback_requires_code(client):
    resp = _callback(client, "nonce|/admin", code="")
    assert resp.status_code == 400


def test_callback_exchange_failure_is_401(client, monkeypatch):
    def boom(_code):
        raise auth.AuthError("nope")

    monkeypatch.setattr(auth, "exchange_code", boom)
    resp = _callback(client, "nonce|/admin")
    assert resp.status_code == 401


def test_callback_mints_token_in_secret_mode(client, monkeypatch):
    monkeypatch.setattr(
        auth,
        "exchange_code",
        lambda _code: {"identity": _identity(), "id_token": "provider", "raw": {}},
    )
    resp = _callback(client, "nonce|/admin")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert 'location.replace("/admin" + \'#token=\' + "' in body
    token = body.split("'#token=' + \"", 1)[1].split('"', 1)[0]
    identity = auth.decode_token(token)
    assert identity.user_id == "alice"
    assert identity.is_admin is True


def test_callback_clears_the_state_cookie(client, monkeypatch):
    monkeypatch.setattr(
        auth,
        "exchange_code",
        lambda _code: {"identity": _identity(), "id_token": "provider", "raw": {}},
    )
    resp = _callback(client, "nonce|/admin")
    assert resp.status_code == 200
    assert f"{STATE_COOKIE}=" in resp.headers["set-cookie"]
    assert "max-age=0" in resp.headers["set-cookie"].lower()


def test_callback_reuses_id_token_in_jwks_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_JWKS_FILE", "/nonexistent/jwks.json")
    monkeypatch.setattr(
        auth,
        "exchange_code",
        lambda _code: {"identity": _identity(), "id_token": "provider-token", "raw": {}},
    )
    resp = _callback(client, "nonce|/admin")
    assert resp.status_code == 200
    assert "provider-token" in resp.text


def test_callback_uses_signed_next_target(client, monkeypatch):
    monkeypatch.setattr(
        auth,
        "exchange_code",
        lambda _code: {"identity": _identity(), "id_token": "provider", "raw": {}},
    )
    resp = _callback(client, "nonce|/admin?tab=files")
    assert resp.status_code == 200
    assert 'location.replace("/admin?tab=files"' in resp.text


def test_callback_falls_back_on_offsite_next(client, monkeypatch):
    monkeypatch.setattr(
        auth,
        "exchange_code",
        lambda _code: {"identity": _identity(), "id_token": "provider", "raw": {}},
    )
    resp = _callback(client, "nonce|//evil.example.com")
    assert resp.status_code == 200
    assert 'location.replace("/admin"' in resp.text
    assert "evil.example.com" not in resp.text


def test_callback_escapes_script_breakout_in_target(client, monkeypatch):
    monkeypatch.setattr(
        auth,
        "exchange_code",
        lambda _code: {"identity": _identity(), "id_token": "provider", "raw": {}},
    )
    # A path such as /admin</script> must not terminate the script block.
    resp = _callback(client, "nonce|/admin</script><script>alert(1)</script>")
    assert resp.status_code == 200
    assert "</script><script>alert(1)" not in resp.text
    assert "\\u003c/script\\u003e" in resp.text


@pytest.mark.parametrize(
    "value",
    ["", "admin", "//evil.example.com", "https://evil.example.com", "/a b", "/admin\n"],
)
def test_safe_next_rejects_offsite_targets(value):
    assert auth._safe_next(value) == "/admin"


def test_safe_next_keeps_same_origin_path():
    assert auth._safe_next("/admin?tab=files") == "/admin?tab=files"


def test_safe_next_strips_existing_fragment():
    assert auth._safe_next("/admin#token=old") == "/admin"


def test_default_departments_fill_missing_claim(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    monkeypatch.setattr(settings, "AUTH_DEFAULT_DEPTS", "Tech, HR")
    try:
        token = auth.encode_token(_identity(departments=()))
        assert auth.decode_token(token).departments == ("Tech", "HR")
    finally:
        monkeypatch.setattr(settings, "AUTH_DEFAULT_DEPTS", "")


def test_denied_role_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    monkeypatch.setattr(settings, "AUTH_CLAIM_ROLES", "groups")
    monkeypatch.setattr(settings, "AUTH_DENY_ROLE", "guest")
    try:
        token = auth.encode_token(_identity(groups=("guest",)))
        with pytest.raises(auth.AuthError):
            auth.decode_token(token)
    finally:
        monkeypatch.setattr(settings, "AUTH_CLAIM_ROLES", "roles")
        monkeypatch.setattr(settings, "AUTH_DENY_ROLE", "")


# --- exchange_code ---------------------------------------------------------


class _Resp:
    def __init__(self, status_code, payload=None, boom=None):
        self.status_code = status_code
        self._payload = payload
        self._boom = boom

    def json(self):
        if self._boom:
            raise self._boom
        return self._payload


def _patch_requests(monkeypatch, response):
    import requests

    # exchange_code verifies the id_token, which needs the HS256 secret.
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", SECRET)
    monkeypatch.setattr(settings, "AUTH_JWKS_FILE", "")
    monkeypatch.setattr(settings, "AUTH_JWKS_URL", "")
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)


def test_exchange_code_returns_identity(monkeypatch):
    _patch_requests(monkeypatch, _Resp(200, {}))
    token = auth.encode_token(_identity())
    _patch_requests(monkeypatch, _Resp(200, {"id_token": token}))
    result = auth.exchange_code("code-1")
    assert result["identity"].user_id == "alice"
    assert result["id_token"] == token


def test_exchange_code_rejects_http_error(monkeypatch):
    _patch_requests(monkeypatch, _Resp(400, {"error": "invalid_grant"}))
    with pytest.raises(auth.AuthError, match="HTTP 400"):
        auth.exchange_code("code-1")


def test_exchange_code_rejects_missing_id_token(monkeypatch):
    _patch_requests(monkeypatch, _Resp(200, {"access_token": "abc"}))
    with pytest.raises(auth.AuthError, match="no id_token"):
        auth.exchange_code("code-1")


def test_exchange_code_wraps_transport_errors(monkeypatch):
    import requests

    def boom(*_a, **_k):
        raise requests.ConnectionError("dns")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(auth.AuthError, match="token exchange failed"):
        auth.exchange_code("code-1")


def test_exchange_code_wraps_non_json_response(monkeypatch):
    _patch_requests(monkeypatch, _Resp(200, boom=ValueError("not json")))
    with pytest.raises(auth.AuthError, match="token exchange failed"):
        auth.exchange_code("code-1")


def test_exchange_code_rejects_token_signed_by_someone_else(monkeypatch):
    _patch_requests(monkeypatch, _Resp(200, {"id_token": "not-a-jwt"}))
    with pytest.raises(auth.AuthError):
        auth.exchange_code("code-1")
