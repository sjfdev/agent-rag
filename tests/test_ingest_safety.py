"""Tier 0 ingestion safety: cancellation, pre-ingest estimate, dry run.

入库安全阀：文档边界取消、入库前预检估算、试跑（不写库）。
"""

import importlib
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import admin_api
from src.config import settings
from src.engine import KnowledgeBaseEngine, get_engine, reset_engine
from src.etl import EnterpriseETL


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    eng = KnowledgeBaseEngine()
    yield eng
    eng.reset()


@pytest.fixture()
def root(tmp_path):
    data = tmp_path / "data"
    for dept in ("Seq", "Par", "Tech"):
        (data / dept).mkdir(parents=True)
    return data


def _write_files(folder: Path, count: int = 3) -> None:
    for i in range(count):
        (folder / f"doc{i}.md").write_text(
            f"# 文档 {i}\n\n第 {i} 份文件的正文，用于验证取消与试跑。\n" * 12,
            encoding="utf-8",
        )


def _seed_for_estimation(test_client, data, dept: str = "Tech"):
    folder = data / dept
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.md").write_text("# A\n\n阿尔法内容。\n" * 20, encoding="utf-8")
    (folder / "b.md").write_text("# B\n\n贝塔内容。\n" * 20, encoding="utf-8")
    job = test_client.post("/admin/api/ingest", json={"dept": dept}).json()
    return _wait_job(test_client, job["job_id"])


def _wait_job(client, job_id: str, timeout: float = 30.0, terminal=None):
    terminal = terminal or {"done", "error", "cancelled"}
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        state = client.get(f"/admin/api/jobs/{job_id}").json()
        if state["status"] in terminal:
            return state
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish: {state}")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "DATA_DIR", str(data))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(settings, "JOBS_PATH", str(tmp_path / "jobs.json"))

    admin_api._job_store.reset()
    with admin_api._cancel_events_lock:
        admin_api._cancel_events.clear()
    reset_engine()
    import main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client, data, get_engine()
    reset_engine()


# -- cancellation -----------------------------------------------------------


def test_sequential_cancel_stops_after_the_current_document(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    _write_files(root / "Seq")
    event = threading.Event()
    etl = EnterpriseETL(data_dir=str(root), prefetch=False, cancel_event=event)
    original = etl._consume

    def consume(plan):
        result = original(plan)
        # Ask to stop after the first document has been fully written.
        event.set()
        return result

    monkeypatch.setattr(etl, "_consume", consume)
    etl.process_department_folder("Seq", str(root / "Seq"))

    assert etl.stats["added"] == 1
    assert etl.stats["chunks"] > 0
    # Only the first document reached the index; the other two were never
    # started, so there is no half-written or stale duplicate.
    summary = engine.doc_summary()
    assert len(summary) == 1


def test_prefetch_cancel_never_writes_the_next_prepared_document(
    engine, root, monkeypatch
):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    _write_files(root / "Par")
    event = threading.Event()
    etl = EnterpriseETL(
        data_dir=str(root), prefetch=True, prefetch_depth=1, cancel_event=event
    )
    original = etl._consume

    def consume(plan):
        result = original(plan)
        event.set()
        return result

    monkeypatch.setattr(etl, "_consume", consume)
    etl.process_department_folder("Par", str(root / "Par"))

    assert etl.stats["added"] == 1
    # A prefetched-but-not-consumed document must be discarded, not written.
    assert len(engine.doc_summary()) == 1


def test_cancelled_progress_stays_midway_and_has_no_eta(monkeypatch):
    monkeypatch.setattr(admin_api, "_count_supported_files", lambda dept=None: 10)
    now = time.time()
    job = {
        "id": "job-cancel",
        "status": "cancelled",
        "dept": None,
        "created": now - 300,
        "started": now - 300,
        "finished": now,
        "log": [
            "  update   公共/A.pdf (10 chunks)",
            "  skip     公共/B.pdf (unchanged)",
            "  update   公共/C.pdf (5 chunks)",
        ],
        "stats": None,
    }
    progress = admin_api._job_progress(job)
    assert progress["processed"] == 3
    assert progress["percent"] == 30.0
    assert progress["eta_seconds"] is None


def test_cancel_endpoint_signals_the_event_and_audits(client):
    test_client, _, _ = client
    job_id = "cancel1"
    admin_api._job_store.create(
        {
            "id": job_id,
            "status": "running",
            "created": time.time(),
            "started": time.time(),
            "finished": None,
            "dept": None,
            "reset": False,
            "log": [],
            "stats": None,
            "error": None,
        }
    )
    event = admin_api._register_cancel_event(job_id)
    try:
        resp = test_client.post(f"/admin/api/jobs/{job_id}/cancel")
        assert resp.status_code == 200, resp.text
        assert resp.json()["cancelling"] is True
        assert event.is_set()
        state = test_client.get(f"/admin/api/jobs/{job_id}").json()
        assert state["cancel_requested"] is True
        audit = test_client.get("/admin/api/audit?limit=5").json()["records"]
        assert any(record["action"] == "ingest.cancel" for record in audit)
    finally:
        admin_api._drop_cancel_event(job_id)


def test_cancel_endpoint_rejects_unknown_and_finished_jobs(client):
    test_client, _, _ = client
    assert test_client.post("/admin/api/jobs/nope/cancel").status_code == 404
    job_id = "done1"
    admin_api._job_store.create(
        {
            "id": job_id,
            "status": "done",
            "created": time.time(),
            "started": time.time(),
            "finished": time.time(),
            "dept": None,
            "reset": False,
            "log": [],
            "stats": {"chunks": 1},
            "error": None,
        }
    )
    assert test_client.post(f"/admin/api/jobs/{job_id}/cancel").status_code == 409


def test_background_job_reaches_cancelled_and_releases_the_lock(client, monkeypatch):
    test_client, data, _ = client
    dept = "Tech"
    folder = data / dept
    folder.mkdir()
    _write_files(folder)

    original = EnterpriseETL._consume

    def slow_consume(self, plan):
        time.sleep(0.25)
        return original(self, plan)

    monkeypatch.setattr(EnterpriseETL, "_consume", slow_consume)
    job = test_client.post("/admin/api/ingest", json={"dept": dept}).json()
    assert _wait_job(test_client, job["job_id"], terminal={"running"})["status"] == "running"
    cancel = test_client.post(f"/admin/api/jobs/{job['job_id']}/cancel")
    assert cancel.status_code == 200, cancel.text
    state = _wait_job(test_client, job["job_id"])
    assert state["status"] == "cancelled"
    assert state["error"] is None
    assert state["progress"]["eta_seconds"] is None
    # The worker releases _ingest_lock after the current document is done.
    assert test_client.get("/admin/api/meta").json()["ingesting"] is False


# -- estimate ---------------------------------------------------------------


def test_estimate_reports_queued_and_skipped_without_touching_the_index(client):
    test_client, data, engine = client
    state = _seed_for_estimation(test_client, data)
    assert state["status"] == "done"

    # Change only one of the two files; the other must be reported as skipped.
    (data / "Tech" / "a.md").write_text("# A\n\n阿尔法内容已更新。\n" * 20, encoding="utf-8")
    before = engine.count()
    estimate = test_client.post("/admin/api/ingest/estimate", json={"dept": "Tech"})
    assert estimate.status_code == 200, estimate.text
    payload = estimate.json()
    assert payload["total_files"] == 2
    assert payload["queued"] == 1
    assert payload["skipped"] == 1
    assert payload["estimated_chunks"] > 0
    assert payload["chunks_per_min"] > 0
    assert payload["baseline"]
    assert engine.count() == before


def test_estimate_reset_treats_every_file_as_queued(client):
    test_client, data, _ = client
    _seed_for_estimation(test_client, data)
    payload = test_client.post(
        "/admin/api/ingest/estimate", json={"reset": True}
    ).json()
    assert payload["total_files"] == 2
    assert payload["queued"] == 2
    assert payload["skipped"] == 0


# -- dry run ----------------------------------------------------------------


def test_dry_run_measures_but_never_writes(client):
    test_client, data, engine = client
    dept = "Dry"
    folder = data / dept
    folder.mkdir()
    _write_files(folder, count=3)
    before = engine.count()

    resp = test_client.post(
        "/admin/api/ingest",
        json={"dept": dept, "dry_run": True, "dry_run_limit": 2},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["dry_run"] is True
    assert report["sampled"] == 2
    assert report["queued"] == 3
    assert len(report["per_doc"]) == 2
    assert all("total_ms" in item and "chunks" in item for item in report["per_doc"])
    assert report["estimated_seconds"] >= 0
    assert report["estimated_chunks"] > 0
    assert engine.count() == before
    # A dry run never creates a persisted job.
    assert test_client.get("/admin/api/jobs").json()["jobs"] == []
