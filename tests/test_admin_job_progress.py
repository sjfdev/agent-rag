"""Progress derivation for the admin ingestion monitor.

管理台「运行监视」的进度计算测试。
"""

import time

import pytest

from src import admin_api


def _job(status="running", log=None, stats=None, started_ago=600):
    now = time.time()
    return {
        "id": "job1",
        "status": status,
        "dept": None,
        "created": now - started_ago,
        "started": now - started_ago,
        "finished": None,
        "log": log or [],
        "stats": stats,
    }


def test_progress_counts_documents_chunks_and_elapsed(monkeypatch):
    monkeypatch.setattr(admin_api, "_count_supported_files", lambda dept=None: 10)
    job = _job(
        log=[
            "  update   公共/A.pdf (10 chunks)",
            "  skip     公共/B.pdf (no extractable text)",
            "  error    公共/C.pdf (ValueError: bad)",
        ]
    )
    progress = admin_api._job_progress(job)
    assert progress["processed"] == 3
    assert progress["total"] == 10
    assert progress["chunks"] == 10
    assert progress["percent"] == 30.0
    assert progress["current"] == "公共/C.pdf"
    assert progress["elapsed_seconds"] == pytest.approx(600, abs=3)
    assert progress["eta_seconds"] is None  # too early to estimate
    assert progress["rate_docs_per_min"] > 0
    assert progress["rate_chunks_per_min"] > 0


def test_progress_estimates_eta_after_forty_percent(monkeypatch):
    monkeypatch.setattr(admin_api, "_count_supported_files", lambda dept=None: 10)
    job = _job(
        log=[
            "  update   公共/A.pdf (10 chunks)",
            "  update   公共/B.pdf (20 chunks)",
            "  skip     公共/C.pdf (unchanged)",
            "  update   公共/D.pdf (30 chunks)",
            "  skip     公共/E.pdf (unchanged)",
        ]
    )
    progress = admin_api._job_progress(job)
    assert progress["processed"] == 5
    assert progress["percent"] == 50.0
    assert progress["eta_seconds"] == pytest.approx(600, rel=0.05)


def test_completed_job_reports_full_progress_and_chunk_total(monkeypatch):
    monkeypatch.setattr(admin_api, "_count_supported_files", lambda dept=None: 10)
    now = time.time()
    job = _job(status="done", stats={"chunks": 42})
    job["started"] = now - 120
    job["finished"] = now
    progress = admin_api._job_progress(job)
    assert progress["processed"] == 10
    assert progress["total"] == 10
    assert progress["percent"] == 100.0
    assert progress["chunks"] == 42
    assert progress["elapsed_seconds"] == pytest.approx(120, abs=3)
    assert progress["eta_seconds"] is None

