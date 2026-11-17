"""Delivery bundle: allowlist, manifest hashes and secret exclusions.

交付包：白名单、清单哈希与敏感文件排除。
"""

import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location(
        "package_release", SCRIPTS / "package_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module():
    return _load()


def test_bundle_contains_only_delivery_files(module, tmp_path):
    from src.version import __version__

    manifest = module.build(tmp_path, keep_staging=True)
    staging = tmp_path / f"agent-rag-{__version__}-delivery"

    assert (staging / "docker-compose.prod.yml").is_file()
    assert (staging / "src" / "license.py").is_file()
    assert (staging / "static" / "index.html").is_file()
    assert (staging / "scripts" / "issue_license.py").is_file()
    assert (staging / "INSTALL.md").is_file()
    assert (staging / "config" / "acl.example.json").is_file()

    # Never ship runtime state, tests, env files or signing keys.
    for forbidden in (
        "tests",
        "data",
        "logs",
        "backups",
        "volumes",
        "dist",
        ".env",
        "config/license_private.pem",
        "config/users.json",
        "monitoring/alertmanager/alertmanager.yml",
        "monitoring/prometheus/metrics_token",
    ):
        assert not (staging / forbidden).exists(), forbidden

    assert manifest["version"] == __version__
    assert manifest["with_image"] is False
    assert manifest["archive"].endswith(".tar.gz")
    assert manifest["files"]


def test_manifest_hashes_and_archive(module, tmp_path):
    manifest = module.build(tmp_path)
    archive = Path(manifest["archive"])
    assert archive.is_file()

    sidecar = Path(manifest["sidecar"])
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert manifest["archive_sha256"] == digest
    assert digest in sidecar.read_text(encoding="utf-8")

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    bundle = manifest["name"]
    assert f"{bundle}/PACKAGE.json" in names
    assert f"{bundle}/INSTALL.md" in names


def test_manifest_sha256_matches_files(module, tmp_path):
    manifest = module.build(tmp_path, keep_staging=True)
    staging = tmp_path / manifest["name"]
    for entry in manifest["files"]:
        path = staging / entry["path"]
        if not path.is_file():
            continue
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]

    written = json.loads((staging / "PACKAGE.json").read_text(encoding="utf-8"))
    assert written["version"] == manifest["version"]
    assert written["files"] == manifest["files"]


def test_existing_bundle_requires_force(module, tmp_path):
    module.build(tmp_path)
    with pytest.raises(SystemExit):
        module.build(tmp_path)
    assert module.build(tmp_path, force=True)["archive"]


def test_bundle_can_embed_a_signed_license(module, tmp_path, monkeypatch):
    import base64
    from datetime import date, timedelta

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from src import license as license_module
    from src.config import settings

    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(settings, "LICENSE_PUBLIC_KEY", base64.b64encode(public).decode())

    payload = {
        "v": 1,
        "license_id": "LIC-PKG-001",
        "licensee": "ACME Corp",
        "edition": "enterprise",
        "issued_at": date.today().isoformat(),
        "expires_at": (date.today() + timedelta(days=365)).isoformat(),
        "max_depts": 3,
        "max_docs": 1000,
        "features": ["sso"],
    }
    signed = tmp_path / "issued.json"
    signed.write_text(
        json.dumps(license_module.sign_payload(payload, key)), encoding="utf-8"
    )

    manifest = module.build(tmp_path / "dist", license_file=str(signed), keep_staging=True)
    staging = tmp_path / "dist" / manifest["name"]
    embedded = staging / "config" / "license.json"

    assert embedded.is_file() and embedded.stat().st_mode & 0o777 == 0o600
    assert manifest["license"] == {
        "licensee": "ACME Corp",
        "license_id": "LIC-PKG-001",
        "expires_at": payload["expires_at"],
    }
    assert "ACME Corp" in (staging / "INSTALL.md").read_text(encoding="utf-8")
    paths = {entry["path"] for entry in manifest["files"]}
    assert "config/license.json" in paths


def test_bundle_rejects_a_tampered_license(module, tmp_path, monkeypatch):
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from src.config import settings

    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(settings, "LICENSE_PUBLIC_KEY", base64.b64encode(public).decode())
    bad = tmp_path / "bad.json"
    bad.write_text('{"payload": {}, "signature": "AAAA"}', encoding="utf-8")

    with pytest.raises(SystemExit):
        module.build(tmp_path / "dist", license_file=str(bad))
