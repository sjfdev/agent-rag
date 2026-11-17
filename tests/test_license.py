"""Offline license: signing, states, grace period and the admin write gate.

离线许可：签名、状态、宽限期与管理端写入闸门。
"""

import base64
import json
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from src import license as license_module
from src.config import settings


@pytest.fixture()
def signing_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def isolated(tmp_path, monkeypatch, signing_key):
    public = signing_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(settings, "LICENSE_ENABLED", True)
    monkeypatch.setattr(settings, "LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.setattr(settings, "LICENSE_PUBLIC_KEY", base64.b64encode(public).decode())
    monkeypatch.setattr(settings, "LICENSE_GRACE_DAYS", 14)
    monkeypatch.setattr(settings, "LICENSE_WARN_DAYS", 30)
    license_module.reset_cache()
    yield tmp_path / "license.json", signing_key
    license_module.reset_cache()


def _payload(**overrides):
    today = license_module._today()
    payload = {
        "v": 1,
        "license_id": "LIC-TEST-001",
        "licensee": "ACME Corp",
        "edition": "enterprise",
        "issued_at": (today - timedelta(days=10)).isoformat(),
        "expires_at": (today + timedelta(days=90)).isoformat(),
        "max_depts": 5,
        "max_docs": 100000,
        "features": ["sso", "audit"],
    }
    payload.update(overrides)
    return payload


def _write(path, key, payload):
    blob = license_module.sign_payload(payload, key)
    path.write_text(json.dumps(blob), encoding="utf-8")
    license_module.reset_cache()
    return blob


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "LICENSE_ENABLED", False)
    state = license_module.status()
    assert state["state"] == "disabled"
    assert state["ok"] is True
    assert license_module.writes_allowed() is True
    license_module.validate_config()  # must not raise


def test_valid_license(isolated):
    path, key = isolated
    _write(path, key, _payload())
    state = license_module.status()
    assert state["state"] == "valid"
    assert state["ok"] is True and state["warn"] is False
    assert state["licensee"] == "ACME Corp"
    assert state["edition"] == "enterprise"
    assert state["max_depts"] == 5 and state["max_docs"] == 100000
    assert state["features"] == ["sso", "audit"]
    assert state["days_left"] == 90
    assert state["expires_at"] == (license_module._today() + timedelta(days=90)).isoformat()


def test_expiring_soon_warns(isolated):
    path, key = isolated
    _write(path, key, _payload(expires_at=(license_module._today() + timedelta(days=5)).isoformat()))
    state = license_module.status()
    assert state["state"] == "valid"
    assert state["warn"] is True
    assert state["days_left"] == 5


def test_grace_period_still_allows_writes(isolated):
    path, key = isolated
    _write(path, key, _payload(expires_at=(license_module._today() - timedelta(days=3)).isoformat()))
    state = license_module.status()
    assert state["state"] == "grace"
    assert state["ok"] is True
    assert state["days_left"] == -3


def test_expired_blocks_writes(isolated):
    path, key = isolated
    _write(
        path,
        key,
        _payload(
            issued_at=(license_module._today() - timedelta(days=400)).isoformat(),
            expires_at=(license_module._today() - timedelta(days=30)).isoformat(),
        ),
    )
    state = license_module.status()
    assert state["state"] == "expired"
    assert state["ok"] is False
    assert license_module.writes_allowed() is False
    # Expiry never stops the process from starting (chat must keep working).
    license_module.validate_config()


def test_tampered_payload_is_invalid(isolated):
    path, key = isolated
    blob = _write(path, key, _payload())
    blob["payload"]["licensee"] = "Evil Corp"
    path.write_text(json.dumps(blob), encoding="utf-8")
    license_module.reset_cache()
    state = license_module.status()
    assert state["state"] == "invalid"
    assert state["ok"] is False
    with pytest.raises(RuntimeError):
        license_module.validate_config()


def test_missing_file_fails_closed(isolated):
    path, _ = isolated
    state = license_module.status()
    assert state["state"] == "missing"
    assert state["ok"] is False
    with pytest.raises(RuntimeError, match="license misconfigured"):
        license_module.validate_config()


def test_not_before_in_future_is_invalid(isolated):
    path, key = isolated
    _write(
        path,
        key,
        _payload(not_before=(license_module._today() + timedelta(days=2)).isoformat()),
    )
    state = license_module.status()
    assert state["state"] == "invalid"
    assert "not valid yet" in state["error"]


def test_structural_errors_raise(isolated):
    path, key = isolated
    payload = _payload(v=2)
    blob = {
        "payload": payload,
        "signature": base64.b64encode(
            key.sign(license_module.canonical_payload(payload))
        ).decode(),
    }
    path.write_text(json.dumps(blob), encoding="utf-8")
    license_module.reset_cache()
    assert license_module.status()["state"] == "invalid"

    with pytest.raises(license_module.LicenseError):
        license_module.verify_blob({"payload": payload})
    with pytest.raises(license_module.LicenseError):
        license_module.verify_blob({"payload": _payload(), "signature": "%%%"})


def test_describe_hides_file_path(isolated):
    path, key = isolated
    _write(path, key, _payload())
    described = license_module.describe()
    assert "file" not in described
    assert described["state"] == "valid"


def test_info_reports_license(isolated):
    path, key = isolated
    _write(path, key, _payload())
    import main

    with TestClient(main.app) as client:
        body = client.get("/info").json()
    assert body["license"]["state"] == "valid"
    assert body["license"]["licensee"] == "ACME Corp"
    assert "file" not in body["license"]


def test_write_gate_blocks_admin_writes_only(isolated, monkeypatch):
    path, key = isolated
    _write(path, key, _payload())
    import main

    monkeypatch.setattr(license_module, "writes_allowed", lambda: False)
    with TestClient(main.app) as client:
        assert client.get("/info").status_code == 200
        blocked = client.post("/admin/api/dept", json={"name": "Tech"})
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"] == "license_expired"


def test_admin_license_endpoint(isolated):
    path, key = isolated
    _write(path, key, _payload())
    import main

    with TestClient(main.app) as client:
        body = client.get("/admin/api/license").json()
    assert body["state"] == "valid"
    assert body["licensee"] == "ACME Corp"
    assert "file" not in body


def test_admin_install_writes_file_with_0600(isolated):
    path, key = isolated
    # The process starts with an expired license (the renewal case), then the
    # console installs a fresh one without a restart.
    _write(
        path,
        key,
        _payload(
            issued_at=(license_module._today() - timedelta(days=400)).isoformat(),
            expires_at=(license_module._today() - timedelta(days=1)).isoformat(),
        ),
    )
    import main

    blob = license_module.sign_payload(_payload(), key)
    with TestClient(main.app) as client:
        response = client.post("/admin/api/license", json={"license": blob})
    assert response.status_code == 200
    body = response.json()
    assert body["license"]["state"] == "valid"
    assert body["payload"]["licensee"] == "ACME Corp"
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    assert license_module.status()["state"] == "valid"


def test_admin_install_rejects_tampered_file(isolated):
    path, key = isolated
    _write(path, key, _payload())
    before = path.read_bytes()
    tampered = license_module.sign_payload(_payload(), key)
    tampered["payload"]["licensee"] = "Evil Corp"
    import main

    with TestClient(main.app) as client:
        response = client.post("/admin/api/license", json={"license": tampered})
    assert response.status_code == 400
    assert path.read_bytes() == before
    assert license_module.status()["state"] == "valid"


def test_renewal_is_allowed_while_expired(isolated):
    path, key = isolated
    _write(
        path,
        key,
        _payload(
            issued_at=(license_module._today() - timedelta(days=400)).isoformat(),
            expires_at=(license_module._today() - timedelta(days=30)).isoformat(),
        ),
    )
    import main

    with TestClient(main.app) as client:
        blocked = client.post("/admin/api/dept", json={"name": "Tech"})
        assert blocked.status_code == 403

        renewal = license_module.sign_payload(_payload(), key)
        renewed = client.post("/admin/api/license", json={"license": renewal})
        assert renewed.status_code == 200
        assert renewed.json()["license"]["state"] == "valid"

        # The write gate lifts immediately; no restart needed.
        assert license_module.writes_allowed() is True
        allowed = client.post("/admin/api/dept", json={"name": "Tech"})
        assert allowed.status_code != 403


def test_install_while_disabled_stores_for_later(isolated, monkeypatch):
    path, key = isolated
    monkeypatch.setattr(settings, "LICENSE_ENABLED", False)
    license_module.reset_cache()
    import main

    blob = license_module.sign_payload(_payload(), key)
    with TestClient(main.app) as client:
        response = client.post("/admin/api/license", json={"license": blob})
    assert response.status_code == 200
    assert response.json()["license"]["state"] == "disabled"
    assert path.is_file()
