"""The doctor script reports problems without raising."""

import importlib.util
import sys
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
