"""The doctor script reports problems without raising."""

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "doctor.py"


@pytest.fixture()
def doctor(monkeypatch):
    spec = importlib.util.spec_from_file_location("doctor_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    yield module
    module.results.clear()


def test_doctor_runs_offline(doctor, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["doctor.py", "--no-llm"])
    code = doctor.main()
    output = capsys.readouterr().out
    assert "Python version" in output
    assert "RESULT:" in output
    assert code in (0, 1)
    assert len(doctor.results) >= 5


def test_doctor_reports_failure_for_missing_data_dir(doctor, monkeypatch, capsys):
    from src.config import settings

    monkeypatch.setattr(sys, "argv", ["doctor.py", "--no-llm"])
    monkeypatch.setattr(settings, "DATA_DIR", "/nonexistent/data-dir")
    code = doctor.main()
    output = capsys.readouterr().out
    assert "[FAIL] Data directory" in output
    assert code == 1


def test_doctor_warns_on_empty_index(doctor, monkeypatch, capsys, tmp_path):
    from src.config import settings

    monkeypatch.setattr(sys, "argv", ["doctor.py", "--no-llm"])
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "empty-db"))
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    (tmp_path / "Dept").mkdir()
    code = doctor.main()
    output = capsys.readouterr().out
    assert "Index" in output
    assert code in (0, 1)


def test_doctor_warns_when_no_backup_exists(doctor, monkeypatch, capsys, tmp_path):
    from src.config import settings

    monkeypatch.setattr(sys, "argv", ["doctor.py", "--no-llm"])
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path / "no-backups"))
    doctor.main()
    output = capsys.readouterr().out
    assert "[WARN] Backup" in output


def test_doctor_reports_backup_age(doctor, monkeypatch, capsys, tmp_path):
    from src.config import settings

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    archive = backup_dir / "agent-rag-backup-20260101-000000.tar.gz"
    archive.write_bytes(b"archive")
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["doctor.py", "--no-llm"])
    monkeypatch.setattr(settings, "BACKUP_DIR", str(backup_dir))

    doctor.main()
    output = capsys.readouterr().out
    assert "[OK  ] Backup" in output

    old = time.time() - 40 * 86400
    os.utime(archive, (old, old))
    doctor.results.clear()
    doctor.main()
    output = capsys.readouterr().out
    assert "[WARN] Backup" in output
    assert "day(s) old" in output


def test_doctor_fails_on_broken_acl(doctor, monkeypatch, capsys, tmp_path):
    from src import acl
    from src.config import settings

    monkeypatch.setattr(sys, "argv", ["doctor.py", "--no-llm"])
    monkeypatch.setattr(settings, "ACL_ENABLED", True)
    monkeypatch.setattr(settings, "ACL_FILE", str(tmp_path / "missing-acl.json"))
    acl.refresh()
    try:
        code = doctor.main()
        output = capsys.readouterr().out
        assert "[FAIL] Document ACL" in output
        assert code == 1
    finally:
        monkeypatch.setattr(settings, "ACL_ENABLED", False)
        acl.refresh()


def test_doctor_fails_when_local_login_has_no_accounts(doctor, monkeypatch, capsys, tmp_path):
    from src import local_auth
    from src.config import settings

    monkeypatch.setattr(sys, "argv", ["doctor.py", "--no-llm"])
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", "x" * 40)
    monkeypatch.setattr(settings, "AUTH_LOCAL_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_USERS_FILE", str(tmp_path / "users.json"))
    local_auth.clear_cache()
    try:
        code = doctor.main()
        output = capsys.readouterr().out
        assert "[FAIL] Local accounts" in output
        assert code == 1
    finally:
        monkeypatch.setattr(settings, "AUTH_LOCAL_LOGIN_ENABLED", False)
        monkeypatch.setattr(settings, "AUTH_MODE", "none")
        local_auth.clear_cache()
