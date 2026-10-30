"""Ingestion job registry with optional persistence.

Jobs are kept in memory for speed and mirrored to a JSON file so the admin
console can still show recent history after a restart. The file is written
atomically (temp + os.replace) and only when jobs change state.
"""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings

logger = logging.getLogger("agent-rag.jobs")

_MAX_JOBS = 20
_MAX_LOG_LINES = 500


def _path() -> Optional[Path]:
    if not settings.JOBS_PATH:
        return None
    return Path(settings.JOBS_PATH)


class JobStore:
    """Thread-safe job registry with best-effort JSON persistence."""

    def __init__(self, max_jobs: int = _MAX_JOBS, max_log_lines: int = _MAX_LOG_LINES):
        self.max_jobs = max_jobs
        self.max_log_lines = max_log_lines
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = _path()
        if not path or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            jobs = data.get("jobs") if isinstance(data, dict) else data
            if isinstance(jobs, list):
                for job in jobs:
                    if isinstance(job, dict) and job.get("id"):
                        self._jobs[job["id"]] = job
                logger.info("restored %d ingestion job(s) from %s", len(self._jobs), path)
        except Exception:
            logger.warning("could not restore job history from %s", path, exc_info=True)

    def _persist(self) -> None:
        path = _path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"jobs": list(self._jobs.values())}, ensure_ascii=False
            )
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".jobs-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except Exception:
            logger.warning("could not persist job history", exc_info=True)

    # -- API ---------------------------------------------------------------
    def create(self, job: Dict[str, Any]) -> None:
        with self._lock:
            self._load()
            self._jobs[job["id"]] = job
            self._prune_locked()
            self._persist()

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            self._load()
            job = self._jobs.get(job_id)
            if not job:
                return
            job.update(fields)
            self._persist()

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            self._load()
            job = self._jobs.get(job_id)
            if not job:
                return
            job.setdefault("log", []).append(line)
            log = job["log"]
            if len(log) > self.max_log_lines:
                del log[: len(log) - self.max_log_lines]
            self._persist()

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._load()
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            self._load()
            jobs = sorted(
                self._jobs.values(), key=lambda job: job.get("created", 0), reverse=True
            )
            return [dict(job) for job in jobs[:limit]]

    def newest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._load()
            if not self._jobs:
                return None
            return dict(max(self._jobs.values(), key=lambda job: job.get("created", 0)))

    def _prune_locked(self) -> None:
        if len(self._jobs) <= self.max_jobs:
            return
        stale = sorted(self._jobs.values(), key=lambda job: job.get("created", 0))
        for job in stale[: len(self._jobs) - self.max_jobs]:
            self._jobs.pop(job["id"], None)

    def reset(self) -> None:
        """Clear all jobs (tests)."""
        with self._lock:
            self._jobs.clear()
            self._loaded = True
            path = _path()
            if path and path.is_file():
                try:
                    path.unlink()
                except Exception:
                    pass


def recover_interrupted_jobs(store: JobStore) -> int:
    """Mark jobs that were running when the process died as failed."""
    changed = 0
    for job in store.list(limit=store.max_jobs):
        if job.get("status") in {"queued", "running"}:
            store.update(
                job["id"],
                status="error",
                error="interrupted by a server restart",
                finished=time.time(),
            )
            store.append_log(job["id"], "ERROR: interrupted by a server restart")
            changed += 1
    return changed
