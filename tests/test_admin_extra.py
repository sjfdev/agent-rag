"""Admin API error paths and the maintenance/rebuild branch."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from src.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import importlib

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "DATA_DIR", str(data))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(settings, "JOBS_PATH", str(tmp_path / "jobs.json"))
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client, data


def _wait(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/admin/api/jobs/{job_id}").json()
        if state["status"] in {"done", "error"}:
            return state
        time.sleep(0.2)
    raise AssertionError("timeout")


def test_unknown_department_paths(client):
    test_client, _ = client
    assert test_client.get("/admin/api/files?dept=Nope").status_code == 404
    resp = test_client.post(
        "/admin/api/upload",
        data={"dept": "Nope", "subdir": ""},
        files={"files": ("a.md", b"x", "text/markdown")},
    )
    assert resp.status_code == 404
    assert test_client.delete("/admin/api/file?dept=Nope&source=Nope/a.md").status_code == 404
    assert test_client.post("/admin/api/ingest", json={"dept": "Nope"}).status_code == 404


def test_job_not_found(client):
    test_client, _ = client
    assert test_client.get("/admin/api/jobs/deadbeef").status_code == 404


def test_reset_cannot_target_one_department(client):
    test_client, data = client
    (data / "Tech").mkdir()
    resp = test_client.post("/admin/api/ingest", json={"dept": "Tech", "reset": True})
    assert resp.status_code == 400


def test_delete_rejects_bad_paths(client):
    test_client, data = client
    (data / "Tech").mkdir()
    assert (
        test_client.delete("/admin/api/file?dept=Tech&source=Tech/../etc/passwd").status_code
        == 400
    )
    assert (
        test_client.delete("/admin/api/file?dept=Tech&source=Tech/missing.md").status_code
        == 404
    )


def test_upload_rejects_invalid_and_legacy_names(client):
    test_client, data = client
    dept = "Names" + uuid.uuid4().hex[:6]
    (data / dept).mkdir()
    test_client.post("/admin/api/dept", json={"name": dept})
    resp = test_client.post(
        "/admin/api/upload",
        data={"dept": dept, "subdir": ""},
        files=[
            ("files", ("~$lock.md", b"x", "text/markdown")),
            ("files", ("old.doc", b"x", "application/msword")),
        ],
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]["errors"]
    assert any("invalid filename" in item["error"] for item in body)
    assert any("legacy .doc" in item["error"] for item in body)


def test_upload_rejects_bad_subdir(client):
    test_client, data = client
    dept = "Sub" + uuid.uuid4().hex[:6]
    (data / dept).mkdir()
    test_client.post("/admin/api/dept", json={"name": dept})
    resp = test_client.post(
        "/admin/api/upload",
        data={"dept": dept, "subdir": "a/../b"},
        files={"files": ("a.md", b"x", "text/markdown")},
    )
    assert resp.status_code == 400


def test_upload_rejects_oversized_single_file(client, monkeypatch):
    test_client, data = client
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)
    dept = "Big" + uuid.uuid4().hex[:6]
    (data / dept).mkdir()
    test_client.post("/admin/api/dept", json={"name": dept})
    resp = test_client.post(
        "/admin/api/upload",
        data={"dept": dept, "subdir": ""},
        files={"files": ("big.md", b"x" * (1024 * 1024 + 10), "text/markdown")},
    )
    assert resp.status_code == 400
    assert "exceeds" in resp.text


def test_ingest_rejects_concurrent_jobs(client, monkeypatch, tmp_path):
    test_client, data = client
    import src.admin_api as admin_api

    (data / "Tech").mkdir()
    assert admin_api._ingest_lock.acquire(blocking=False)
    try:
        resp = test_client.post("/admin/api/ingest", json={"dept": "Tech"})
        assert resp.status_code == 409
    finally:
        admin_api._ingest_lock.release()


def test_full_reset_uses_shadow_and_reports_done(client, monkeypatch):
    test_client, data = client
    (data / "Tech").mkdir()
    (data / "Tech" / "doc.md").write_text("# Doc\n\nrebuild me")
    calls = {"begin": 0, "commit": 0, "abort": 0}

    import src.admin_api as admin_api

    real = admin_api.kb_engine
    monkeypatch.setattr(real, "begin_rebuild", lambda: calls.__setitem__("begin", calls["begin"] + 1))
    monkeypatch.setattr(real, "commit_rebuild", lambda: calls.__setitem__("commit", calls["commit"] + 1))
    monkeypatch.setattr(real, "abort_rebuild", lambda: calls.__setitem__("abort", calls["abort"] + 1))
    monkeypatch.setattr(admin_api, "begin_maintenance", lambda: None)
    monkeypatch.setattr(admin_api, "end_maintenance", lambda: None)
    monkeypatch.setattr(real, "hybrid_mode", "milvus", raising=False)

    job = test_client.post("/admin/api/ingest", json={"reset": True}).json()
    state = _wait(test_client, job["job_id"])
    assert state["status"] == "done", state
    assert calls == {"begin": 1, "commit": 1, "abort": 0}
    assert any("Server-side sparse BM25" in line for line in state["log"])


def test_failed_rebuild_aborts_and_keeps_previous(client, monkeypatch):
    test_client, data = client
    (data / "Tech").mkdir()
    (data / "Tech" / "doc.md").write_text("# Doc\n\nboom")
    calls = {"abort": 0}

    import src.admin_api as admin_api

    real = admin_api.kb_engine
    monkeypatch.setattr(real, "begin_rebuild", lambda: None)
    monkeypatch.setattr(real, "commit_rebuild", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
    monkeypatch.setattr(real, "abort_rebuild", lambda: calls.__setitem__("abort", calls["abort"] + 1))
    monkeypatch.setattr(admin_api, "begin_maintenance", lambda: None)
    monkeypatch.setattr(admin_api, "end_maintenance", lambda: None)

    job = test_client.post("/admin/api/ingest", json={"reset": True}).json()
    state = _wait(test_client, job["job_id"])
    assert state["status"] == "error"
    assert calls["abort"] == 1
    assert "previous index is still live" in " ".join(state["log"])


def test_ingest_without_departments_fails(client, monkeypatch):
    test_client, data = client
    import shutil

    shutil.rmtree(data, ignore_errors=True)
    data.mkdir()
    job = test_client.post("/admin/api/ingest", json={}).json()
    state = _wait(test_client, job["job_id"])
    assert state["status"] == "error"
    assert "no department folders" in state["error"]


def test_list_jobs_shape(client):
    test_client, data = client
    (data / "Tech").mkdir()
    job = test_client.post("/admin/api/ingest", json={"dept": "Tech"}).json()
    _wait(test_client, job["job_id"])
    jobs = test_client.get("/admin/api/jobs").json()["jobs"]
    assert jobs and {"id", "status", "dept", "created", "finished"} <= set(jobs[0])


def test_audit_limit_is_clamped(client):
    test_client, _ = client
    assert test_client.get("/admin/api/audit?limit=9999").status_code == 200
    assert test_client.get("/admin/api/audit?limit=0").status_code == 200
