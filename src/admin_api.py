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

from src import audit
from src.config import settings
from src.engine import kb_engine
from src.etl import EnterpriseETL, doc_id_for
from src.hashing import clear_hash_cache, file_sha1
from src.jobs import JobStore
from src.loaders import LEGACY_EXTENSIONS, SUPPORTED_EXTENSIONS
from src.metrics import observe_ingest_job
from src.runtime import begin_maintenance, end_maintenance
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
    return file_sha1(path)


def _describe_file(
    path: Path,
    dept: str,
    supported: bool,
    summary: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
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
        if summary is None:
            summary = kb_engine.doc_summary()
        entry = summary.get(doc_id)
        item["chunks"] = int(entry.get("chunks", 0)) if entry else 0
        existing = entry.get("doc_hash") if entry else None
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
    try:
        summary = kb_engine.doc_summary()
    except Exception:
        logger.warning("could not load doc summary", exc_info=True)
        summary = {}
    for name in depts:
        folder = root / name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.name.startswith((".", "~$")):
                continue
            supported = path.suffix.lower() in SUPPORTED_EXTENSIONS
            yield _describe_file(path, name, supported, summary)


class DeptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class IngestRequest(BaseModel):
    dept: Optional[str] = None
    reset: bool = False


_job_store = JobStore(max_jobs=_MAX_JOBS, max_log_lines=_MAX_LOG_LINES)
_ingest_lock = threading.Lock()


def _job_update(job_id: str, **fields: Any) -> None:
    _job_store.update(job_id, **fields)


def _job_log(job_id: str, line: str) -> None:
    _job_store.append_log(job_id, line)


def job_snapshot() -> Optional[Dict[str, Any]]:
    """Copy of the newest ingestion job, used by /health."""
    newest = _job_store.newest()
    if not newest:
        return None
    return {
        "id": newest.get("id"),
        "dept": newest.get("dept"),
        "reset": newest.get("reset"),
        "status": newest.get("status"),
        "started": newest.get("started"),
        "finished": newest.get("finished"),
        "error": newest.get("error"),
    }


def _actor(request: Request) -> str:
    return audit.actor_label(bool(settings.ADMIN_TOKEN), request.client.host if request.client else "")


def _run_job(job_id: str, dept: Optional[str], reset: bool) -> None:
    started = time.time()
    _job_update(job_id, status="running", started=started)
    rebuilding = False
    try:
        if reset:
            # Build into a shadow index: queries keep serving the old data and
            # the switch happens only after every document is written.
            _job_log(job_id, "Building a shadow index (live queries stay online)...")
            kb_engine.begin_rebuild()
            rebuilding = True

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

        if reset and not keep:
            raise RuntimeError(
                "rebuild produced no documents; keeping the previous index"
            )

        if not dept:
            pruned = etl.prune(keep)
            if pruned:
                _job_log(job_id, f"Pruned {pruned} document(s) no longer present.")

        if rebuilding:
            _job_log(job_id, "Switching over to the rebuilt index...")
            # Only the (millisecond) switch is a maintenance window now.
            begin_maintenance()
            try:
                kb_engine.commit_rebuild()
            finally:
                end_maintenance()
            rebuilding = False
            _job_log(job_id, "Rebuild committed; previous index dropped.")

        if kb_engine.hybrid_mode == "local":
            try:
                depts = kb_engine.warm_lexical(set(departments))
                _job_log(job_id, f"Lexical index warmed for: {', '.join(depts) or '-'}")
            except Exception:
                logger.warning("lexical warmup after ingest failed", exc_info=True)
        elif kb_engine.hybrid_mode == "milvus":
            _job_log(job_id, "Server-side sparse BM25 index updated by Milvus.")

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
        if rebuilding:
            try:
                kb_engine.abort_rebuild()
                _job_log(job_id, "Rebuild aborted; the previous index is still live.")
            except Exception:
                logger.exception("could not abort rebuild")
        _job_update(
            job_id, status="error", error=str(exc), finished=time.time()
        )
        observe_ingest_job("error")
    finally:
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
        "max_upload_total_mb": settings.MAX_UPLOAD_TOTAL_MB,
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
def create_dept(req: DeptRequest, request: Request):
    name = validate_dept(req.name)
    path = _resolve_under_data(_data_root() / name)
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    audit.log_event(
        "dept.create", _actor(request), dept=name, detail={"created": not existed}
    )
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
    request: Request,
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
    total_limit = settings.MAX_UPLOAD_TOTAL_MB * 1024 * 1024
    declared = 0
    for upload in files:
        upload.file.seek(0, 2)
        declared += upload.file.tell()
        upload.file.seek(0)
    if total_limit and declared > total_limit:
        raise HTTPException(
            status_code=413,
            detail=(
                f"upload is {declared / 1024 / 1024:.1f}MB; at most "
                f"{settings.MAX_UPLOAD_TOTAL_MB}MB per request"
            ),
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

    # Overwrites reuse (path, mtime, size) keys rarely, but clearing keeps the
    # cache honest when a file is replaced within the same mtime granularity.
    clear_hash_cache()
    for item in saved:
        audit.log_event(
            "file.upload",
            _actor(request),
            dept=dept,
            source=item["source"],
            detail={"size": item["size"], "overwritten": item["overwritten"]},
        )
    for error in errors:
        audit.log_event(
            "file.upload",
            _actor(request),
            dept=dept,
            source=str(error.get("name")),
            ok=False,
            detail={"error": error.get("error")},
        )
    if not saved and errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"saved": saved, "errors": errors}


@router.delete("/file")
def delete_file(dept: str, source: str, request: Request):
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

    clear_hash_cache()
    removed = kb_engine.delete_by_doc_id(doc_id_for(dept, source_rel))
    audit.log_event(
        "file.delete",
        _actor(request),
        dept=dept,
        source=source_rel,
        detail={"chunks_removed": removed},
    )
    return {"deleted": source, "chunks_removed": removed}


@router.post("/ingest")
def start_ingest(req: IngestRequest, request: Request):
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
    _job_store.create(
        {
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
    )
    audit.log_event(
        "ingest.start",
        _actor(request),
        dept=dept,
        detail={"job_id": job_id, "reset": req.reset},
    )
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
    job = _job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/jobs")
def list_jobs():
    jobs = [
        {
            "id": job["id"],
            "status": job["status"],
            "dept": job.get("dept"),
            "created": job.get("created"),
            "finished": job.get("finished"),
        }
        for job in _job_store.list(limit=10)
    ]
    return {"jobs": jobs}


@router.get("/audit")
def list_audit(limit: int = 50):
    limit = max(1, min(limit, 500))
    return {"records": audit.tail(limit)}
