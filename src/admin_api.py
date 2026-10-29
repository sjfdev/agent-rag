"""Admin API: upload, inspect, delete and (re)index knowledge base files.

Security model:
  * If ADMIN_TOKEN is set, every request must send it in X-Admin-Token.
  * If it is empty, the API only accepts loopback clients (127.0.0.1 / ::1).
"""

import logging
import re
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from src.config import settings
from src.engine import kb_engine
from src.etl import EnterpriseETL, doc_id_for
from src.runtime import begin_maintenance, end_maintenance
from src.metrics import observe_ingest_job
from src.loaders import LEGACY_EXTENSIONS, SUPPORTED_EXTENSIONS
from src.validation import validate_dept

logger = logging.getLogger("agent-rag.admin")

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5 ]{1,64}$")
_MAX_FILES_PER_UPLOAD = 50
_MAX_LOG_LINES = 500
_MAX_JOBS = 20


def require_admin(request: Request) -> None:
    token = settings.ADMIN_TOKEN
    if token:
        provided = request.headers.get("X-Admin-Token", "")
        if not secrets.compare_digest(provided, token):
            raise HTTPException(status_code=401, detail="invalid admin token")
        return
    host = request.client.host if request.client else ""
    if host not in _LOCAL_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="admin API is loopback-only; set ADMIN_TOKEN to allow remote access",
        )


router = APIRouter(
    prefix="/admin/api", tags=["admin"], dependencies=[Depends(require_admin)]
)


def _data_root() -> Path:
    return Path(settings.DATA_DIR)


def _list_depts() -> List[str]:
    root = _data_root()
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name not in {"etc"}
    )


def _safe_segment(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _SAFE_SEGMENT.fullmatch(value) or value in {".", ".."}:
        raise HTTPException(status_code=400, detail=f"invalid {label}")
    return value


def _resolve_under_data(path: Path) -> Path:
    root = _data_root().resolve()
    resolved = path.resolve()
    if root != resolved and root not in resolved.parents:
        raise HTTPException(status_code=400, detail="path outside data directory")
    return resolved


def _source_of(path: Path) -> str:
    root = _data_root().resolve()
    return path.resolve().relative_to(root).as_posix()


def _file_hash(path: Path) -> str:
    import hashlib

    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _describe_file(path: Path, dept: str, supported: bool) -> Dict[str, Any]:
    source = _source_of(path)
    stat = path.stat()
    item: Dict[str, Any] = {
        "dept": dept,
        "source": source,
        "name": path.name,
        "ext": path.suffix.lower(),
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "status": "unsupported",
        "chunks": 0,
    }
    if not supported:
        return item

    doc_id = doc_id_for(dept, source)
    item["doc_id"] = doc_id
    try:
        existing = kb_engine.get_doc_hash(doc_id)
        item["chunks"] = kb_engine.count_by_doc_id(doc_id)
        if existing is None:
            item["status"] = "pending"
        elif existing == _file_hash(path):
            item["status"] = "indexed"
        else:
            item["status"] = "changed"
    except Exception:
        logger.warning("failed to inspect %s", source, exc_info=True)
        item["status"] = "unknown"
    return item


def _iter_files(dept: Optional[str] = None):
    root = _data_root().resolve()
    depts = [dept] if dept else _list_depts()
    for name in depts:
        folder = root / name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.name.startswith((".", "~$")):
                continue
            supported = path.suffix.lower() in SUPPORTED_EXTENSIONS
            yield _describe_file(path, name, supported)


class DeptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class IngestRequest(BaseModel):
    dept: Optional[str] = None
    reset: bool = False


_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_ingest_lock = threading.Lock()


def _job_update(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)


def _job_log(job_id: str, line: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["log"].append(line)
        if len(job["log"]) > _MAX_LOG_LINES:
            del job["log"][: len(job["log"]) - _MAX_LOG_LINES]


def job_snapshot() -> Optional[Dict[str, Any]]:
    """Copy of the newest ingestion job, used by /health."""
    with _jobs_lock:
        if not _jobs:
            return None
        newest = max(_jobs.values(), key=lambda job: job["created"])
        return {
            "id": newest.get("id"),
            "dept": newest.get("dept"),
            "reset": newest.get("reset"),
            "status": newest.get("status"),
            "started": newest.get("started"),
            "finished": newest.get("finished"),
            "error": newest.get("error"),
        }


def _prune_jobs() -> None:
    with _jobs_lock:
        if len(_jobs) <= _MAX_JOBS:
            return
        for job_id in sorted(_jobs, key=lambda k: _jobs[k]["created"])[:-_MAX_JOBS]:
            _jobs.pop(job_id, None)


def _run_job(job_id: str, dept: Optional[str], reset: bool) -> None:
    started = time.time()
    _job_update(job_id, status="running", started=started)
    try:
        if reset:
            _job_log(job_id, "Dropping the existing index/collection...")
            begin_maintenance()
            kb_engine.reset()

        etl = EnterpriseETL(
            data_dir=settings.DATA_DIR, on_log=lambda m: _job_log(job_id, m)
        )
        root = _data_root()
        departments = [dept] if dept else _list_depts()
        if not departments:
            raise RuntimeError(f"no department folders under {settings.DATA_DIR}")

        keep = set()
        for name in departments:
            _job_log(job_id, f"[{name}]")
            keep |= etl.process_department_folder(name, str(root / name))

        if not dept:
            pruned = etl.prune(keep)
            if pruned:
                _job_log(job_id, f"Pruned {pruned} document(s) no longer present.")

        if settings.HYBRID_ENABLED:
            try:
                depts = kb_engine.warm_lexical(set(departments))
                _job_log(job_id, f"Lexical index warmed for: {', '.join(depts) or '-'}")
            except Exception:
                logger.warning("lexical warmup after ingest failed", exc_info=True)

        stats = dict(etl.stats)
        _job_log(
            job_id,
            "Done. docs: added={added} updated={updated} skipped={skipped} "
            "pruned={pruned} failed={failed}; chunks={chunks}".format(**stats),
        )
        _job_update(job_id, status="done", stats=stats, finished=time.time())
        observe_ingest_job("done")
    except Exception as exc:
        logger.exception("ingestion job %s failed", job_id)
        _job_log(job_id, f"ERROR: {exc}")
        _job_update(
            job_id, status="error", error=str(exc), finished=time.time()
        )
        observe_ingest_job("error")
    finally:
        if reset:
            end_maintenance()
        _ingest_lock.release()


@router.get("/meta")
def admin_meta():
    counts = {"indexed": 0, "pending": 0, "changed": 0, "unsupported": 0, "unknown": 0}
    per_dept: Dict[str, int] = {}
    for item in _iter_files():
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        per_dept[item["dept"]] = per_dept.get(item["dept"], 0) + 1
    return {
        "vector_store": settings.VECTOR_STORE_TYPE,
        "embedding_backend": settings.EMBEDDING_BACKEND,
        "llm_model": settings.LLM_MODEL,
        "data_dir": str(_data_root().resolve()),
        "max_upload_mb": settings.MAX_UPLOAD_MB,
        "formats": sorted(SUPPORTED_EXTENSIONS),
        "legacy_formats": sorted(LEGACY_EXTENSIONS),
        "depts": [{"name": d, "files": per_dept.get(d, 0)} for d in _list_depts()],
        "counts": counts,
        "ingesting": _ingest_lock.locked(),
    }


@router.get("/files")
def list_files(dept: Optional[str] = None):
    if dept:
        dept = validate_dept(dept)
        if dept not in _list_depts():
            raise HTTPException(status_code=404, detail="department not found")
    files = list(_iter_files(dept))
    summary: Dict[str, int] = {}
    for item in files:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    return {"dept": dept, "files": files, "counts": summary}


@router.post("/dept")
def create_dept(req: DeptRequest):
    name = validate_dept(req.name)
    path = _resolve_under_data(_data_root() / name)
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    return {"name": name, "created": not existed}


def _save_upload(upload: UploadFile, dest: Path) -> int:
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    tmp = dest.with_name(dest.name + ".part")
    size = 0
    try:
        with tmp.open("wb") as handle:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"{dest.name} exceeds {settings.MAX_UPLOAD_MB}MB",
                    )
                handle.write(chunk)
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink()
        upload.file.close()
    return size


@router.post("/upload")
def upload_files(
    dept: str = Form(...),
    subdir: str = Form(""),
    files: List[UploadFile] = File(...),
):
    dept = validate_dept(dept)
    if dept not in _list_depts():
        raise HTTPException(status_code=404, detail="department not found")
    if not files:
        raise HTTPException(status_code=400, detail="no files uploaded")
    if len(files) > _MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=400, detail=f"at most {_MAX_FILES_PER_UPLOAD} files per upload"
        )

    folder = _data_root() / dept
    if subdir.strip():
        for segment in subdir.replace("\\", "/").split("/"):
            if segment:
                folder = folder / _safe_segment(segment, "subdirectory")
    folder = _resolve_under_data(folder)
    folder.mkdir(parents=True, exist_ok=True)

    saved, errors = [], []
    for upload in files:
        raw_name = Path(upload.filename or "").name.strip()
        if not raw_name or raw_name.startswith((".", "~$")):
            errors.append({"name": upload.filename, "error": "invalid filename"})
            continue
        suffix = Path(raw_name).suffix.lower()
        if suffix in LEGACY_EXTENSIONS:
            errors.append(
                {
                    "name": raw_name,
                    "error": f"legacy {suffix}; convert to {LEGACY_EXTENSIONS[suffix]}",
                }
            )
            continue
        if suffix not in SUPPORTED_EXTENSIONS:
            errors.append({"name": raw_name, "error": f"unsupported type {suffix}"})
            continue
        dest = _resolve_under_data(folder / raw_name)
        existed = dest.exists()
        try:
            size = _save_upload(upload, dest)
        except HTTPException as exc:
            errors.append({"name": raw_name, "error": exc.detail})
            continue
        except Exception as exc:
            logger.exception("upload failed for %s", raw_name)
            errors.append({"name": raw_name, "error": str(exc)})
            continue
        saved.append(
            {
                "name": raw_name,
                "source": _source_of(dest),
                "size": size,
                "overwritten": existed,
            }
        )

    if not saved and errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"saved": saved, "errors": errors}


@router.delete("/file")
def delete_file(dept: str, source: str):
    dept = validate_dept(dept)
    if dept not in _list_depts():
        raise HTTPException(status_code=404, detail="department not found")
    parts = [p for p in source.replace("\\", "/").split("/") if p]
    if not parts or any(p in {".", ".."} for p in parts):
        raise HTTPException(status_code=400, detail="invalid source path")
    if parts[0] == dept:
        parts = parts[1:]
    if not parts:
        raise HTTPException(status_code=400, detail="invalid source path")
    path = _resolve_under_data(_data_root() / dept / Path(*parts))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    source_rel = _source_of(path)
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot delete file: {exc}")

    removed = kb_engine.delete_by_doc_id(doc_id_for(dept, source_rel))
    return {"deleted": source, "chunks_removed": removed}


@router.post("/ingest")
def start_ingest(req: IngestRequest):
    if req.dept:
        dept = validate_dept(req.dept)
        if dept not in _list_depts():
            raise HTTPException(status_code=404, detail="department not found")
    else:
        dept = None
    if req.reset and dept:
        raise HTTPException(
            status_code=400, detail="reset must target the whole knowledge base"
        )
    if not _ingest_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="an ingestion job is already running")

    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "created": time.time(),
            "started": None,
            "finished": None,
            "dept": dept,
            "reset": req.reset,
            "log": [],
            "stats": None,
            "error": None,
        }
    _prune_jobs()
    try:
        threading.Thread(
            target=_run_job, args=(job_id, dept, req.reset), daemon=True
        ).start()
    except Exception:
        _ingest_lock.release()
        raise
    return {"job_id": job_id, "dept": dept, "reset": req.reset}


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return dict(job)


@router.get("/jobs")
def list_jobs():
    with _jobs_lock:
        jobs = [
            {
                "id": job["id"],
                "status": job["status"],
                "dept": job["dept"],
                "created": job["created"],
                "finished": job["finished"],
            }
            for job in sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
        ]
    return {"jobs": jobs[:10]}
