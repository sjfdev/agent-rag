"""OIDC/JWKS verification and claim mapping (Keycloak/Feishu-style tokens)."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src import auth
from src.config import settings


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def jwks_file(tmp_path, monkeypatch, rsa_key):
    public = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key())
    )
    public.update({"kid": "test-key-1", "alg": "RS256", "use": "sig"})
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps({"keys": [public]}))
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWKS_FILE", str(path))
    monkeypatch.setattr(settings, "AUTH_JWKS_URL", "")
    monkeypatch.setattr(settings, "AUTH_JWT_ISSUER", "")
    monkeypatch.setattr(settings, "AUTH_JWT_AUDIENCE", "")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", "")
    auth.clear_jwks_cache()
    yield path
    monkeypatch.setattr(settings, "AUTH_JWKS_FILE", "")
    monkeypatch.setattr(settings, "AUTH_MODE", "none")
    auth.clear_jwks_cache()


def _sign(rsa_key, claims, kid="test-key-1"):
    return jwt.encode(
        claims, rsa_key, algorithm="RS256", headers={"kid": kid}
    )


def _claims(**overrides):
    base = {
        "sub": "user-123",
        "exp": int(time.time()) + 300,
        "depts": ["Tech"],
        "groups": ["exec"],
        "admin": False,
    }
    base.update(overrides)
    return base


def test_rs256_token_verified_with_jwks(jwks_file, rsa_key):
    identity = auth.decode_token(_sign(rsa_key, _claims()))
    assert identity.user_id == "user-123"
    assert identity.departments == ("Tech",)
    assert identity.groups == ("exec",)
    assert identity.is_admin is False


def test_wrong_key_rejected(jwks_file, rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(_claims(), other, algorithm="RS256", headers={"kid": "test-key-1"})
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)


def test_unknown_kid_is_rejected(jwks_file, rsa_key):
    token = _sign(rsa_key, _claims(), kid="other")
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)


def test_expired_token_rejected(jwks_file, rsa_key):
    token = _sign(rsa_key, _claims(exp=int(time.time()) - 10))
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)


def test_keycloak_style_claims(jwks_file, rsa_key, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_CLAIM_DEPTS", "dept")
    monkeypatch.setattr(settings, "AUTH_CLAIM_GROUPS", "realm_access.roles")
    monkeypatch.setattr(settings, "AUTH_CLAIM_ROLES", "realm_access.roles")
    monkeypatch.setattr(settings, "AUTH_ADMIN_ROLE", "kb-admin")
    monkeypatch.setattr(settings, "AUTH_ROLE_DEPT_PREFIX", "kb:dept:")
    token = _sign(
        rsa_key,
        _claims(
            depts=None,
            groups=None,
            dept=["HR"],
            realm_access={"roles": ["kb-admin", "kb:dept:Finance", "offline_access"]},
        ),
    )
    identity = auth.decode_token(token)
    assert identity.departments == ("HR", "Finance")
    assert identity.is_admin is True
    assert "kb-admin" in identity.groups


def test_role_prefix_without_admin_role(jwks_file, rsa_key, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_CLAIM_ROLES", "realm_access.roles")
    monkeypatch.setattr(settings, "AUTH_ROLE_DEPT_PREFIX", "kb:dept:")
    token = _sign(
        rsa_key, _claims(realm_access={"roles": ["kb:dept:Tech", "kb:dept:HR"]})
    )
    identity = auth.decode_token(token)
    assert identity.departments == ("Tech", "HR")
    assert identity.is_admin is False


def test_missing_subject_rejected(jwks_file, rsa_key):
    token = _sign(rsa_key, {"exp": int(time.time()) + 60})
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)


def test_jwks_config_does_not_require_secret(monkeypatch, jwks_file):
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", "")
    auth.validate_config()  # must not raise


def test_no_jwks_and_no_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWKS_FILE", "")
    monkeypatch.setattr(settings, "AUTH_JWKS_URL", "")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", "")
    with pytest.raises(RuntimeError):
        auth.validate_config()


def test_corrupt_jwks_file_fails_closed(jwks_file, rsa_key, monkeypatch):
    jwks_file.write_text("not json")
    auth.clear_jwks_cache()
    with pytest.raises(auth.AuthError):
        auth.decode_token(_sign(rsa_key, _claims()))


def test_claimed_alg_none_is_rejected(jwks_file):
    token = jwt.encode(_claims(), key="", algorithm="none", headers={"kid": "test-key-1"})
    with pytest.raises(auth.AuthError):
        auth.decode_token(token)
