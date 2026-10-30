"""Audit log writer."""

import json

import pytest

from src import audit


@pytest.fixture()
def audit_path(tmp_path, monkeypatch):
    from src.config import settings

    path = tmp_path / "audit.log"
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(path))
    return path


def test_log_event_writes_json_line(audit_path):
    audit.log_event("file.delete", "loopback:127.0.0.1", dept="Tech", source="Tech/a.md")
    line = audit_path.read_text().strip()
    record = json.loads(line)
    assert record["action"] == "file.delete"
    assert record["dept"] == "Tech"
    assert record["source"] == "Tech/a.md"
    assert record["ok"] is True
    assert record["ts"].endswith("+00:00")


def test_tail_returns_latest_records(audit_path):
    for i in range(5):
        audit.log_event(f"action.{i}", "loopback:127.0.0.1")
    records = audit.tail(limit=3)
    assert [r["action"] for r in records] == ["action.2", "action.3", "action.4"]


def test_tail_missing_file_returns_empty(audit_path):
    assert audit.tail() == []


def test_actor_label_never_contains_the_token(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "ADMIN_TOKEN", "super-secret")
    label = audit.actor_label(True, "10.0.0.1")
    assert "super-secret" not in label
    assert label.startswith("token:")
    assert audit.actor_label(False, "127.0.0.1") == "loopback:127.0.0.1"


def test_write_failure_does_not_raise(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", "/proc/definitely/not/writable.log")
    audit.log_event("file.delete", "loopback:127.0.0.1")  # must not raise
