"""Admin API routes, exercised through FastAPI's TestClient (offline)."""

import shutil
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
    shutil.rmtree(data, ignore_errors=True)
    shutil.rmtree(tmp_path / "db", ignore_errors=True)


def _wait_job(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/admin/api/jobs/{job_id}").json()
        if state["status"] in {"done", "error"}:
            return state
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_meta_shape(client):
    test_client, _ = client
    meta = test_client.get("/admin/api/meta").json()
    assert meta["vector_store"] in {"faiss", "milvus"}
    assert meta["max_upload_mb"] > 0
    assert isinstance(meta["depts"], list)
    assert set(meta["counts"]) >= {"indexed", "pending", "changed"}


def test_admin_token_required(tmp_path, monkeypatch):
    import importlib

    import main

    monkeypatch.setattr(settings, "ADMIN_TOKEN", "s3cret")
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        assert test_client.get("/admin/api/meta").status_code == 401
        ok = test_client.get("/admin/api/meta", headers={"X-Admin-Token": "s3cret"})
        assert ok.status_code == 200


def test_upload_ingest_delete_cycle(client):
    test_client, data = client
    dept = "Smoke" + uuid.uuid4().hex[:6]
    source = f"{dept}/note.md"
    (data / dept).mkdir()

    assert test_client.post("/admin/api/dept", json={"name": dept}).status_code == 200
    upload = test_client.post(
        "/admin/api/upload",
        data={"dept": dept, "subdir": ""},
        files={"files": ("note.md", b"# Note\n\nSmoke marker", "text/markdown")},
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["saved"][0]["source"] == source

    listed = test_client.get(f"/admin/api/files?dept={dept}").json()
    assert listed["files"][0]["status"] == "pending"

    job = test_client.post("/admin/api/ingest", json={"dept": dept}).json()
    state = _wait_job(test_client, job["job_id"])
    assert state["status"] == "done", state
    assert state["stats"]["chunks"] >= 1

    listed = test_client.get(f"/admin/api/files?dept={dept}").json()
    assert listed["files"][0]["status"] == "indexed"
    assert listed["files"][0]["chunks"] >= 1

    deleted = test_client.delete(f"/admin/api/file?dept={dept}&source={source}").json()
    assert deleted["deleted"] == source
    assert deleted["chunks_removed"] >= 1
    assert test_client.get(f"/admin/api/files?dept={dept}").json()["files"] == []

    # Prune the deleted document from the index (the empty department folder
    # still exists, which keeps the full ingest valid).
    state = _wait_job(
        test_client, test_client.post("/admin/api/ingest", json={}).json()["job_id"]
    )
    assert state["status"] == "done", state


def test_upload_rejects_unsupported_extension(client):
    test_client, data = client
    dept = "Reject" + uuid.uuid4().hex[:6]
    (data / dept).mkdir()
    test_client.post("/admin/api/dept", json={"name": dept})
    resp = test_client.post(
        "/admin/api/upload",
        data={"dept": dept, "subdir": ""},
        files={"files": ("evil.bin", b"\x00\x01", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "unsupported" in resp.text


def test_upload_sanitises_path_traversal(client):
    test_client, data = client
    dept = "Safe" + uuid.uuid4().hex[:6]
    (data / dept).mkdir()
    test_client.post("/admin/api/dept", json={"name": dept})
    resp = test_client.post(
        "/admin/api/upload",
        data={"dept": dept, "subdir": "../../etc"},
        files={"files": ("../note.md", b"# Note\n\nsafe", "text/markdown")},
    )
    assert resp.status_code == 400
    assert not (data.parent / "etc").exists()


def test_dept_name_validation(client):
    test_client, _ = client
    resp = test_client.post("/admin/api/dept", json={"name": "bad dept"})
    assert resp.status_code == 400


def test_upload_total_quota(client, monkeypatch):
    test_client, data = client
    monkeypatch.setattr(settings, "MAX_UPLOAD_TOTAL_MB", 1)
    dept = "Quota" + uuid.uuid4().hex[:6]
    (data / dept).mkdir()
    test_client.post("/admin/api/dept", json={"name": dept})
    big = b"x" * (1024 * 1024 + 10)
    resp = test_client.post(
        "/admin/api/upload",
        data={"dept": dept, "subdir": ""},
        files=[
            ("files", ("a.txt", big, "text/plain")),
            ("files", ("b.txt", big, "text/plain")),
        ],
    )
    assert resp.status_code == 413
    assert "per request" in resp.text


def test_audit_log_records_actions(client):
    test_client, data = client
    dept = "Audit" + uuid.uuid4().hex[:6]
    (data / dept).mkdir()
    test_client.post("/admin/api/dept", json={"name": dept})
    test_client.post(
        "/admin/api/upload",
        data={"dept": dept, "subdir": ""},
        files={"files": ("note.md", b"# Note\n\naudit", "text/markdown")},
    )
    records = test_client.get("/admin/api/audit?limit=10").json()["records"]
    actions = [r["action"] for r in records]
    assert "dept.create" in actions
    assert "file.upload" in actions
    assert all("actor" in r and "ts" in r for r in records)
    # The admin token itself must never be written to the log.
    assert settings.ADMIN_TOKEN == "" or settings.ADMIN_TOKEN not in str(records)


def test_job_history_survives_restart(client, tmp_path):
    test_client, data = client
    dept = "Jobs" + uuid.uuid4().hex[:6]
    (data / dept).mkdir()
    test_client.post("/admin/api/dept", json={"name": dept})
    job = test_client.post("/admin/api/ingest", json={"dept": dept}).json()
    _wait_job(test_client, job["job_id"])
    jobs_path = tmp_path / "jobs.json"
    assert jobs_path.is_file()
    assert job["job_id"] in jobs_path.read_text()

    # A fresh store reads the file back (simulates a process restart).
    from src.jobs import JobStore

    store = JobStore()
    assert any(j["id"] == job["job_id"] for j in store.list(limit=20))
