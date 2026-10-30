"""Job store persistence and interrupted-job recovery."""

import json
import time

import pytest

from src.jobs import JobStore, recover_interrupted_jobs


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "JOBS_PATH", str(tmp_path / "jobs.json"))
    s = JobStore(max_jobs=3, max_log_lines=5)
    yield s
    s.reset()


def _job(job_id, created=None, status="queued"):
    return {
        "id": job_id,
        "status": status,
        "created": created if created is not None else time.time(),
        "started": None,
        "finished": None,
        "dept": None,
        "reset": False,
        "log": [],
        "stats": None,
        "error": None,
    }


def test_create_update_get(store):
    store.create(_job("a"))
    store.update("a", status="running")
    assert store.get("a")["status"] == "running"
    assert store.get("missing") is None


def test_log_is_bounded(store):
    store.create(_job("a"))
    for i in range(20):
        store.append_log("a", f"line {i}")
    log = store.get("a")["log"]
    assert len(log) == 5
    assert log[-1] == "line 19"


def test_prune_keeps_newest(store):
    for i in range(5):
        store.create(_job(f"j{i}", created=100 + i))
    ids = [job["id"] for job in store.list(limit=10)]
    assert ids == ["j4", "j3", "j2"]


def test_persists_across_instances(store, tmp_path):
    store.create(_job("a"))
    store.update("a", status="done")
    again = JobStore()
    assert again.get("a")["status"] == "done"


def test_newest_returns_latest(store):
    store.create(_job("old", created=1))
    store.create(_job("new", created=2))
    assert store.newest()["id"] == "new"


def test_recover_marks_running_jobs_failed(store):
    store.create(_job("stuck", created=1, status="running"))
    store.create(_job("done", created=2, status="done"))
    assert recover_interrupted_jobs(store) == 1
    assert store.get("stuck")["status"] == "error"
    assert "restart" in store.get("stuck")["error"]
    assert store.get("done")["status"] == "done"


def test_missing_file_is_not_an_error(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "JOBS_PATH", str(tmp_path / "nope.json"))
    store = JobStore()
    assert store.list() == []
    assert store.newest() is None


def test_corrupt_file_is_ignored(tmp_path, monkeypatch):
    from src.config import settings

    path = tmp_path / "jobs.json"
    path.write_text("{not json")
    monkeypatch.setattr(settings, "JOBS_PATH", str(path))
    store = JobStore()
    assert store.list() == []
    store.create(_job("a"))
    assert json.loads(path.read_text())["jobs"][0]["id"] == "a"
