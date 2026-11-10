"""Backup/restore scripts: archive layout, verification and safe swapping."""

import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def modules():
    # restore.py does `import backup`, so register the same instance first.
    sys.modules.pop("backup", None)
    backup_mod = _load("backup", "backup.py")
    sys.modules["backup"] = backup_mod
    restore_mod = _load("restore", "restore.py")
    yield backup_mod, restore_mod
    sys.modules.pop("backup", None)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    from src.config import settings

    root = tmp_path / "state"
    data = root / "data"
    (data / "Tech").mkdir(parents=True)
    (data / "Tech" / "policy.txt").write_text("backed up policy", encoding="utf-8")
    (data / "Finance").mkdir()
    (data / "Finance" / "budget.csv").write_text("dept,amount\n", encoding="utf-8")

    db = root / "db"
    db.mkdir()
    (db / "index.faiss").write_bytes(b"faiss-index")

    config = root / "config"
    config.mkdir()
    (config / "users.json").write_text('{"users": {}}', encoding="utf-8")

    logs = root / "logs"
    logs.mkdir()
    (logs / "ingest_jobs.json").write_text("[]", encoding="utf-8")
    (logs / "admin_audit.log").write_text("", encoding="utf-8")

    monkeypatch.setattr(settings, "DATA_DIR", str(data))
    monkeypatch.setattr(settings, "DB_PATH", str(db))
    monkeypatch.setattr(settings, "VECTOR_STORE_TYPE", "faiss")
    monkeypatch.setattr(settings, "BACKUP_VECTOR_PATHS", "")
    monkeypatch.setattr(settings, "AUTH_USERS_FILE", str(config / "users.json"))
    monkeypatch.setattr(settings, "ACL_FILE", str(config / "acl.json"))
    monkeypatch.setattr(settings, "JOBS_PATH", str(logs / "ingest_jobs.json"))
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(logs / "admin_audit.log"))
    return {"root": root, "data": data, "db": db, "config": config, "logs": logs}


def _make_backup(backup_mod, tmp_path):
    return backup_mod.create_backup(dest=tmp_path / "backups", probe_url="")


def test_backup_writes_archive_manifest_and_checksums(modules, state, tmp_path):
    backup_mod, _ = modules
    archive = _make_backup(backup_mod, tmp_path)

    assert archive.is_file()
    assert archive.with_name(archive.name + ".sha256").is_file()
    assert backup_mod.verify_archive(archive, quiet=True) == []

    with tarfile.open(archive, "r:gz") as tar:
        manifest = backup_mod.read_manifest(tar)
    entries = {entry["archive"]: entry for entry in manifest["entries"]}
    assert {"data", "config", "logs", "vector/faiss"} <= set(entries)
    assert entries["data"]["path"] == str(state["data"].resolve())
    assert entries["vector/faiss"]["path"] == str(state["db"].resolve())
    assert manifest["vector_store"] == "faiss"
    assert manifest["files"] >= 4


def test_backup_skips_missing_data_dir(modules, state, tmp_path, monkeypatch):
    from src.config import settings

    backup_mod, _ = modules
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "missing-data"))
    archive = _make_backup(backup_mod, tmp_path)
    assert backup_mod.verify_archive(archive, quiet=True) == []
    with tarfile.open(archive, "r:gz") as tar:
        manifest = backup_mod.read_manifest(tar)
    assert "data" not in {entry["archive"] for entry in manifest["entries"]}


def test_verify_rejects_a_tampered_sidecar(modules, tmp_path):
    backup_mod, _ = modules
    archive = backup_mod.create_backup(dest=tmp_path / "backups", probe_url="")
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")
    errors = backup_mod.verify_archive(archive, quiet=True)
    assert any("checksum mismatch" in error for error in errors)


def test_verify_rejects_archive_without_manifest(modules, tmp_path):
    backup_mod, _ = modules
    bad = tmp_path / "no-manifest.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        payload = tarfile.TarInfo("data/policy.txt")
        payload.size = 1
        tar.addfile(payload, io.BytesIO(b"x"))
    errors = backup_mod.verify_archive(bad, quiet=True)
    assert any("manifest.json" in error for error in errors)


def test_verify_rejects_path_traversal(modules, tmp_path):
    backup_mod, _ = modules
    bad = tmp_path / "traversal.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        payload = tarfile.TarInfo("../evil.txt")
        payload.size = 1
        tar.addfile(payload, io.BytesIO(b"x"))
    errors = backup_mod.verify_archive(bad, quiet=True)
    assert any("unsafe member path" in error for error in errors)


def test_backup_cli_verify_exit_codes(modules, tmp_path):
    backup_mod, _ = modules
    archive = backup_mod.create_backup(dest=tmp_path / "backups", probe_url="")
    assert backup_mod.main(["--verify", str(archive)]) == 0
    with archive.open("ab") as handle:
        handle.write(b"garbage")
    assert backup_mod.main(["--verify", str(archive)]) == 1


def test_restore_roundtrip_keeps_previous_copy(modules, state, tmp_path):
    backup_mod, restore_mod = modules
    archive = _make_backup(backup_mod, tmp_path)

    policy = state["data"] / "Tech" / "policy.txt"
    policy.write_text("modified after backup", encoding="utf-8")
    added = state["data"] / "Tech" / "added.txt"
    added.write_text("not in the backup", encoding="utf-8")

    result = restore_mod.restore_archive(archive, probe_url="", keep_old=True)

    assert result["restored"] is True
    assert policy.read_text(encoding="utf-8") == "backed up policy"
    assert not added.exists()
    previous = [(target, old) for target, old in result["previous"] if old]
    assert previous
    old_data = Path(previous[0][1])
    assert (old_data / "Tech" / "added.txt").exists()
    assert (old_data / "Tech" / "policy.txt").read_text(encoding="utf-8") == (
        "modified after backup"
    )


def test_restore_dry_run_touches_nothing(modules, state, tmp_path):
    backup_mod, restore_mod = modules
    archive = _make_backup(backup_mod, tmp_path)
    policy = state["data"] / "Tech" / "policy.txt"
    policy.write_text("modified after backup", encoding="utf-8")

    result = restore_mod.restore_archive(archive, probe_url="", dry_run=True)

    assert result["restored"] is False
    assert policy.read_text(encoding="utf-8") == "modified after backup"
    assert not list(state["root"].glob("*.pre-restore-*"))


def test_restore_refuses_when_api_is_reachable(modules, state, tmp_path, monkeypatch):
    backup_mod, restore_mod = modules
    archive = _make_backup(backup_mod, tmp_path)
    monkeypatch.setattr(restore_mod, "service_reachable", lambda *args, **kwargs: True)

    with pytest.raises(restore_mod.RestoreError, match="API is reachable"):
        restore_mod.restore_archive(archive, probe_url="http://127.0.0.1:8000/info")

    result = restore_mod.restore_archive(
        archive, probe_url="http://127.0.0.1:8000/info", force=True, dry_run=True
    )
    assert result["restored"] is False


def test_backup_requires_stopped_milvus_upfront(modules, tmp_path, monkeypatch):
    backup_mod, _ = modules
    from src.config import settings

    milvus = tmp_path / "volumes" / "milvus"
    milvus.mkdir(parents=True)
    (milvus / "server_metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "BACKUP_VECTOR_PATHS", str(milvus))
    monkeypatch.setattr(backup_mod, "running_vector_containers", lambda: ["milvus-standalone"])

    with pytest.raises(backup_mod.BackupError, match="containers are running"):
        backup_mod.create_backup(dest=tmp_path / "backups", probe_url="")

    archive = backup_mod.create_backup(dest=tmp_path / "backups", live=True, probe_url="")
    assert backup_mod.verify_archive(archive, quiet=True) == []


def test_restore_rejects_a_conflicting_manifest(modules, state, tmp_path):
    _, restore_mod = modules
    with pytest.raises(restore_mod.RestoreError):
        restore_mod._plan(
            {
                "entries": [
                    {"archive": "data", "path": str(state["root"] / "x")},
                    {"archive": "data", "path": str(state["root"] / "y")},
                ]
            }
        )
