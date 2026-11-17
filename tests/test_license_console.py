"""Vendor license console: signing form, verification and the HTTP surface.

厂商许可控制台：签发表单、验签与本地 HTTP 接口。
"""

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import license_console
from src import license as license_module
from src.config import settings


@pytest.fixture()
def signing_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def console(tmp_path, monkeypatch, signing_key):
    key_path = tmp_path / "license_private.pem"
    key_path.write_bytes(
        signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    monkeypatch.setattr(settings, "LICENSE_ENABLED", True)
    monkeypatch.setattr(settings, "LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.setattr(settings, "LICENSE_PUBLIC_KEY", "")
    license_module.reset_cache()
    yield license_console.ConsoleState(str(key_path), str(tmp_path / "licenses"))
    license_module.reset_cache()


def test_key_info_reads_the_pem(console, signing_key):
    info = console.key_info()
    assert info["exists"] is True and info["error"] is None
    expected = signing_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert info["public_key"] == base64.b64encode(expected).decode()


def test_key_info_reports_missing_key(tmp_path):
    state = license_console.ConsoleState(str(tmp_path / "nope.pem"), str(tmp_path / "out"))
    info = state.key_info()
    assert info["exists"] is False and "init-keys" in info["error"]


def test_issue_form_writes_a_verifiable_file(console):
    result = license_console.issue_form(
        console,
        {
            "licensee": "ACME 演示/客户",
            "days": "365",
            "depts": "3",
            "docs": "5000",
            "features": "sso, audit",
        },
    )
    target = console.out_dir / result["file"]
    assert target.is_file()
    assert target.stat().st_mode & 0o777 == 0o600
    assert "/" not in result["file"] and " " not in result["file"]

    payload = license_module.verify_blob(
        json.loads(target.read_text(encoding="utf-8")), console.public_key()
    )
    assert payload["licensee"] == "ACME 演示/客户"
    assert payload["max_depts"] == 3 and payload["max_docs"] == 5000
    assert payload["features"] == ["sso", "audit"]
    assert result["payload"]["expires_at"] == payload["expires_at"]


def test_issue_form_requires_licensee(console):
    with pytest.raises(ValueError):
        license_console.issue_form(console, {"days": "30"})


def test_issue_form_honours_explicit_expiry(console):
    result = license_console.issue_form(
        console, {"licensee": "ACME", "expires": "2031-01-31"}
    )
    assert result["payload"]["expires_at"] == "2031-01-31"


def test_list_licenses_marks_tampered_files(console):
    license_console.issue_form(console, {"licensee": "ACME", "days": "30"})
    items = console.list_licenses()
    assert len(items) == 1 and items[0]["state"] == "valid"

    path = console.out_dir / items[0]["file"]
    blob = json.loads(path.read_text(encoding="utf-8"))
    blob["payload"]["licensee"] = "EVIL"
    path.write_text(json.dumps(blob), encoding="utf-8")

    items = console.list_licenses()
    assert items[0]["state"] == "invalid"
    assert "invalid" in items[0]["error"]


def test_verify_text_rejects_garbage(console):
    with pytest.raises(ValueError):
        license_console.verify_text("not json", console.public_key())
    with pytest.raises(license_module.LicenseError):
        license_console.verify_text('{"payload": {}, "signature": "x"}', console.public_key())


def test_page_embeds_token_but_never_the_private_key(console):
    page = license_console.build_page(console)
    assert console.token in page
    assert "PRIVATE KEY" not in page
    assert console.public_key() in page


class _Server:
    def __init__(self, state):
        self.state = state
        handler = type("TestHandler", (license_console.Handler,), {"state": state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, *, token=None, data=None, method=None):
        headers = {}
        body = None
        if token:
            headers["X-Console-Token"] = token
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.url + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


@pytest.fixture()
def server(console):
    srv = _Server(console)
    yield srv
    srv.close()


def test_http_requires_the_token(server, console):
    assert server.request("/api/state")[0] == 403
    assert server.request("/api/state", token="wrong")[0] == 403
    code, body = server.request("/api/state", token=console.token)
    assert code == 200 and json.loads(body)["key"]["exists"] is True

    code, body = server.request("/api/issue", data={"licensee": "ACME"})
    assert code == 403 and not (console.out_dir / "x").exists()


def test_http_page_is_public(server):
    code, body = server.request("/")
    assert code == 200 and b"<html" in body.lower()


def test_http_issue_then_download(server, console):
    code, body = server.request(
        "/api/issue", token=console.token, data={"licensee": "ACME", "days": "30"}
    )
    assert code == 200
    issued = json.loads(body)
    assert (console.out_dir / issued["file"]).is_file()

    code, raw = server.request(
        f"/api/download?name={issued['file']}", token=console.token
    )
    assert code == 200
    blob = json.loads(raw)
    assert "signature" in blob

    assert server.request("/api/download?name=../license.json", token=console.token)[0] == 404


def test_http_issue_validates_input(server, console):
    code, body = server.request("/api/issue", token=console.token, data={"days": "30"})
    assert code == 400 and "required" in json.loads(body)["error"]

    code, body = server.request("/api/verify", token=console.token, data={"text": "nope"})
    assert code == 400


def test_http_verify_accepts_issued_blob(server, console):
    _code, body = server.request(
        "/api/issue", token=console.token, data={"licensee": "ACME", "days": "30"}
    )
    issued = json.loads(body)
    code, raw = server.request(
        "/api/verify",
        token=console.token,
        data={"text": json.dumps(issued["blob"])},
    )
    assert code == 200
    assert json.loads(raw)["payload"]["licensee"] == "ACME"


def test_download_rejects_missing_name(server, console):
    assert server.request("/api/download", token=console.token)[0] == 404


def test_slug_falls_back_and_truncates():
    assert license_console._slug("") == "license"
    assert license_console._slug("ACME 演示") == "ACME"
    assert len(license_console._slug("x" * 100)) == 40
