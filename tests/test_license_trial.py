"""Trial licenses: short defaults, soft warning window and the 90-day cap.

试用许可：默认 30 天、7 天提醒窗口与 90 天硬上限。
"""

import base64
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import license_console
from src import license as license_module
from src.config import settings

ROOT = Path(__file__).resolve().parent.parent
ISSUE_SCRIPT = ROOT / "scripts" / "issue_license.py"


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
    today = date.today()
    payload = {
        "v": 1,
        "license_id": "LIC-TRIAL-001",
        "licensee": "ACME Trial",
        "edition": "trial",
        "issued_at": today.isoformat(),
        "expires_at": (today + timedelta(days=30)).isoformat(),
        "max_depts": 1,
        "max_docs": 5000,
        "features": ["sso", "audit", "monitoring"],
    }
    payload.update(overrides)
    return payload


def _write(path: Path, key: Ed25519PrivateKey, payload: dict) -> dict:
    blob = license_module.sign_payload(payload, key)
    path.write_text(json.dumps(blob), encoding="utf-8")
    license_module.reset_cache()
    return blob


def test_trial_status_flags_trial(isolated):
    path, key = isolated
    _write(path, key, _payload())
    state = license_module.status()
    assert state["state"] == "valid"
    assert state["trial"] is True
    assert state["edition"] == "trial"
    assert state["warn"] is False  # 30 days is outside the 7-day trial window
    assert state["max_depts"] == 1 and state["max_docs"] == 5000


def test_trial_warns_in_the_last_week(isolated):
    path, key = isolated
    _write(path, key, _payload(expires_at=(date.today() + timedelta(days=5)).isoformat()))
    state = license_module.status()
    assert state["trial"] is True and state["warn"] is True


def test_trial_longer_than_90_days_is_rejected(isolated):
    path, key = isolated
    payload = _payload(expires_at=(date.today() + timedelta(days=120)).isoformat())
    with pytest.raises(license_module.LicenseError, match="90 days"):
        license_module.sign_payload(payload, key)

    # A hand-crafted signature with a too-long trial is invalid at verification.
    blob = {
        "payload": payload,
        "signature": base64.b64encode(
            key.sign(license_module.canonical_payload(payload))
        ).decode(),
    }
    path.write_text(json.dumps(blob), encoding="utf-8")
    license_module.reset_cache()
    state = license_module.status()
    assert state["state"] == "invalid"
    assert "90 days" in state["error"]


def test_validate_config_warns_for_trial(isolated, caplog):
    path, key = isolated
    _write(path, key, _payload())
    with caplog.at_level("WARNING"):
        license_module.validate_config()
    assert "trial license" in caplog.text


def _load_issue_script():
    spec = importlib.util.spec_from_file_location("issue_license_script", ISSUE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_trial_defaults_and_signature(tmp_path, monkeypatch, capsys):
    module = _load_issue_script()
    key_path = tmp_path / "key.pem"
    assert module.init_keys(str(key_path)) == 0
    private = license_module.load_private_key(str(key_path))
    public = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    # The vendor embeds the public key before shipping; the self-check inside
    # `issue` uses this setting (the compiled key stays the default).
    monkeypatch.setattr(settings, "LICENSE_PUBLIC_KEY", public)

    out = tmp_path / "trial.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "issue_license.py",
            "--trial",
            "--licensee",
            "ACME Trial",
            "--key",
            str(key_path),
            "--out",
            str(out),
        ],
    )
    assert module.main() == 0
    assert "trial license" in capsys.readouterr().out

    blob = json.loads(out.read_text(encoding="utf-8"))
    payload = blob["payload"]
    assert payload["edition"] == "trial"
    assert payload["max_depts"] == 1
    assert payload["max_docs"] == 5000
    assert payload["features"] == ["sso", "audit", "monitoring"]
    issued = date.fromisoformat(payload["issued_at"])
    expires = date.fromisoformat(payload["expires_at"])
    assert (expires - issued).days == 30

    assert license_module.verify_blob(blob, public)["licensee"] == "ACME Trial"


def test_cli_trial_caps_days(tmp_path, monkeypatch, capsys):
    module = _load_issue_script()
    key_path = tmp_path / "key.pem"
    assert module.init_keys(str(key_path)) == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "issue_license.py",
            "--trial",
            "--days",
            "120",
            "--licensee",
            "ACME Trial",
            "--key",
            str(key_path),
            "--out",
            str(tmp_path / "trial.json"),
        ],
    )
    assert module.main() == 1
    assert "90 days" in capsys.readouterr().err


def test_console_refuses_a_long_trial(tmp_path, signing_key):
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    state = license_console.ConsoleState(str(key_path), str(tmp_path / "out"))
    with pytest.raises((ValueError, license_module.LicenseError), match="90 days"):
        license_console.issue_form(
            state,
            {"licensee": "ACME", "edition": "trial", "days": "120"},
        )
