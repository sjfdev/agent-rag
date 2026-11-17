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

from src import audit, auth, chunk_settings, chunking, local_auth, model_settings
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
    # The admin API is never meant to be called from a browser origin other
    # than the console itself; CORS middleware does not cover this rule.
    origin = request.headers.get("origin", "")
    if origin:
        allowed = {
            item.strip()
            for item in (getattr(settings, "CORS_ORIGINS", "") or "").split(",")
            if item.strip()
        }
        host = request.headers.get("host", "")
        if origin not in allowed and host and origin.split("://", 1)[-1] != host:
            raise HTTPException(status_code=403, detail="cross-origin admin request")
    # Break glass first: a matching static token always works (emergency).
    token = settings.ADMIN_TOKEN
    provided = request.headers.get("X-Admin-Token", "")
    if token and secrets.compare_digest(provided, token):
        return

    if auth.auth_enabled():
        # In jwt mode the loopback shortcut is disabled: only admin tokens or
        # the static break-glass token are accepted.
        try:
            identity = auth.identity_from_headers(request.headers)
        except auth.AuthError as exc:
            raise HTTPException(
                status_code=401,
                detail="invalid or missing token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        if identity is not None and identity.is_admin:
            return
        raise HTTPException(status_code=403, detail="admin privileges required")

    if token:
        raise HTTPException(status_code=401, detail="invalid admin token")

    host = request.client.host if request.client else ""
    if host not in _LOCAL_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="admin API is loopback-only; set ADMIN_TOKEN to allow remote access",
        )


router = APIRouter(
    prefix="/admin/api", tags=["admin"], dependencies=[Depends(require_admin)]
)

# The session probe has its own auth handling: it must answer 401 vs 403
# precisely so the console can tell "no token" from "token but not an admin".
session_router = APIRouter(prefix="/admin/api", tags=["admin"])


@session_router.get("/session")
def session_info(request: Request):
    if not auth.auth_enabled():
        return {
            "auth_mode": "none",
            "authenticated": True,
            "is_admin": True,
            "user_id": None,
            "depts": [],
            "groups": [],
            "source": "none",
        }

    # Break-glass static token is accepted regardless of the bearer token.
    token = settings.ADMIN_TOKEN
    provided = request.headers.get("X-Admin-Token", "")
    if token and secrets.compare_digest(provided, token):
        return {
            "auth_mode": "jwt",
            "authenticated": True,
            "is_admin": True,
            "user_id": "admin-token",
            "depts": [],
            "groups": [],
            "source": "admin_token",
        }

    try:
        identity = auth.identity_from_headers(request.headers)
    except auth.AuthError:
        raise HTTPException(
            status_code=401,
            detail="invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "auth_mode": "jwt",
        "authenticated": True,
        "is_admin": identity.is_admin,
        "user_id": identity.user_id,
        "depts": list(identity.departments),
        "groups": list(identity.groups),
        "source": "token",
    }


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
        else:
            # doc_hash also carries the chunking signature: changing a chunking
            # parameter marks indexed files as "changed" until the next ingest.
            # doc_hash 内含分片签名：改分片参数后旧文档显示“有变更”，待重切。
            expected = chunking.document_hash(_file_hash(path), chunk_settings.effective())
            item["status"] = "indexed" if existing == expected else "changed"
    except Exception:
        logger.warning("failed to inspect %s", source, exc_info=True)
        item["status"] = "unknown"
    return item


def _iter_files(dept: Optional[str] = None):
    root = _data_root().resolve()
    depts = [dept] if dept else _list_depts()
    try:
        summary = kb_engine.doc_summary()
    except Exception as exc:
        # Failing loudly beats reporting every file as "pending": when Milvus
        # cannot answer queries the console must say so, not look empty.
        logger.warning("could not load doc summary", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail=f"vector store unavailable: {type(exc).__name__}",
        ) from exc
    for name in depts:
        folder = root / name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.name.startswith((".", "~$")):
                continue
            supported = path.suffix.lower() in SUPPORTED_EXTENSIONS
            yield _describe_file(path, name, supported, summary)


_JOB_ACTION_RE = re.compile(r"^\s*(add|update|skip|error)\s+(.+?)(?:\s+\(|$)")
_JOB_CHUNK_RE = re.compile(r"\((\d+) chunks\)")
_TOTAL_FILES_CACHE = {"key": None, "value": 0, "at": 0.0}


def _count_supported_files(dept: Optional[str] = None) -> int:
    """Supported files on disk for a department (or all departments).

    Cached briefly: the monitor polls the job endpoint, and walking a large
    data directory on every poll would be wasteful.
    """
    key = dept or ""
    now = time.monotonic()
    if _TOTAL_FILES_CACHE["key"] == key and now - _TOTAL_FILES_CACHE["at"] < 5:
        return int(_TOTAL_FILES_CACHE["value"])
    root = _data_root().resolve()
    depts = [dept] if dept else _list_depts()
    total = 0
    for name in depts:
        folder = root / name
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if (
                path.is_file()
                and not path.name.startswith((".", "~$"))
                and path.suffix.lower() in SUPPORTED_EXTENSIONS
            ):
                total += 1
    _TOTAL_FILES_CACHE.update({"key": key, "value": total, "at": now})
    return total


def _job_progress(job: Dict[str, Any]) -> Dict[str, Any]:
    """Derive human-readable progress from a persisted job record.

    The ingestion thread writes one log line per file; parsing that log avoids
    changing the job-store schema for every processed file and also works for
    jobs that were already running when this monitor was added.
    """
    log = job.get("log") or []
    processed = 0
    chunks = 0
    current = ""
    for line in log:
        match = _JOB_ACTION_RE.match(str(line))
        if not match:
            continue
        processed += 1
        current = match.group(2).strip()
        chunk_match = _JOB_CHUNK_RE.search(str(line))
        if chunk_match:
            chunks += int(chunk_match.group(1))

    stats = job.get("stats") or {}
    if stats:
        chunks = int(stats.get("chunks") or chunks)

    total = _count_supported_files(job.get("dept"))
    status = str(job.get("status") or "queued")
    started = job.get("started") or job.get("created")
    finished = job.get("finished")
    now = time.time()
    elapsed = 0.0
    if started:
        elapsed = max(0.0, float(finished or now) - float(started))
    if status == "done":
        processed = total or processed
        percent = 100.0
    else:
        percent = (processed / total * 100.0) if total else 0.0
    percent = max(0.0, min(100.0, percent))

    eta = None
    if status == "running" and total > processed and processed > 0 and elapsed > 0:
        # Early progress is biased by small files; only estimate after 40%.
        if processed >= max(5, int(total * 0.4)):
            eta = elapsed * (total - processed) / processed

    minutes = elapsed / 60.0 if elapsed else 0.0
    return {
        "processed": processed,
        "total": total,
        "percent": round(percent, 1),
        "chunks": chunks,
        "elapsed_seconds": round(elapsed, 1),
        "eta_seconds": round(eta, 1) if eta is not None else None,
        "rate_docs_per_min": round(processed / minutes, 1) if minutes else 0.0,
        "rate_chunks_per_min": round(chunks / minutes, 1) if minutes else 0.0,
        "current": current,
    }


class DeptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class IngestRequest(BaseModel):
    dept: Optional[str] = None
    reset: bool = False


class LicenseInstallRequest(BaseModel):
    """The parsed contents of a vendor-issued license file."""

    license: Dict[str, Any]


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    # Required unless AUTH_DEFAULT_DEPTS supplies one; enforced in local_auth so
    # both the API and the CLI share the rule.
    depts: List[str] = Field(default_factory=list, max_length=20)
    groups: List[str] = Field(default_factory=list, max_length=50)
    admin: bool = False


class UserUpdateRequest(BaseModel):
    """Every field optional: only the ones present are applied."""

    password: Optional[str] = Field(default=None, max_length=256)
    depts: Optional[List[str]] = Field(default=None, max_length=20)
    groups: Optional[List[str]] = Field(default=None, max_length=50)
    admin: Optional[bool] = None
    disabled: Optional[bool] = None


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
    """Who performed the action: the token subject when available, else host."""
    if auth.auth_enabled():
        try:
            identity = auth.identity_from_headers(request.headers)
        except auth.AuthError:
            identity = None
        if identity is not None:
            return f"user:{identity.user_id}"
        provided = request.headers.get("X-Admin-Token", "")
        if settings.ADMIN_TOKEN and secrets.compare_digest(provided, settings.ADMIN_TOKEN):
            return "token:break-glass"
    return audit.actor_label(
        bool(settings.ADMIN_TOKEN), request.client.host if request.client else ""
    )


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
        _job_log(job_id, f"Chunking: {etl.chunk_description()}")
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


@router.get("/license")
def admin_license():
    """Offline license state plus the limits the console should surface."""
    from src import license as license_module

    state = license_module.status()
    state.pop("file", None)
    return state


@router.post("/license")
def install_license(req: LicenseInstallRequest, request: Request):
    """Verify and install a license without touching the filesystem by hand.

    Allowed even while the current license is expired (the write gate in
    ``main.py`` exempts this path), which is exactly the renewal flow. The
    signature is checked first, so a bad upload never replaces a working file.
    """
    from src import license as license_module

    try:
        result = license_module.install(req.license)
    except license_module.LicenseError as exc:
        audit.log_event("license.install", _actor(request), ok=False, detail={"error": str(exc)})
        raise _bad_request(exc)
    except OSError as exc:
        audit.log_event("license.install", _actor(request), ok=False, detail={"error": str(exc)})
        raise HTTPException(status_code=500, detail=f"cannot write license file: {exc}")

    payload = result["payload"]
    audit.log_event(
        "license.install",
        _actor(request),
        detail={
            "licensee": payload.get("licensee"),
            "license_id": payload.get("license_id"),
            "expires_at": payload.get("expires_at"),
        },
    )
    state = dict(result["license"])
    state.pop("file", None)
    return {"license": state, "payload": payload}


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
        "llm_model": model_settings.effective()["model"],
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
    data = dict(job)
    data["progress"] = _job_progress(data)
    return data


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

# --- local accounts (AUTH_LOCAL_LOGIN_ENABLED) -----------------------------
# Only mounted when local login is enabled; otherwise these answer 404, so an
# IdP-only deployment exposes no account store at all.


def _require_local_auth() -> None:
    if not local_auth.enabled():
        raise HTTPException(status_code=404, detail="local login is not configured")


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/users")
def list_users():
    _require_local_auth()
    return {"users": local_auth.list_users(), **local_auth.stats()}


@router.post("/users")
def create_user(req: UserCreateRequest, request: Request):
    _require_local_auth()
    try:
        created = local_auth.create_user(
            req.username,
            req.password,
            depts=req.depts,
            groups=req.groups,
            admin=req.admin,
        )
    except local_auth.LocalAuthError as exc:
        raise _bad_request(exc)
    audit.log_event(
        "user.create",
        _actor(request),
        detail={
            "username": created["username"],
            "admin": created["admin"],
            "depts": created["depts"],
        },
    )
    return created


@router.patch("/users/{username}")
def update_user(username: str, req: UserUpdateRequest, request: Request):
    _require_local_auth()
    changes = req.model_dump(exclude_unset=True)
    password = changes.pop("password", None)

    existing = local_auth.get_user(username)
    if existing is None:
        raise HTTPException(status_code=404, detail="user not found")
    _guard_last_admin(existing, changes)
    # Validate everything before writing: a bad department must not leave the
    # password already rotated.
    try:
        local_auth.validate_update(**changes)
        if password:
            local_auth.validate_password(password)
    except local_auth.LocalAuthError as exc:
        raise _bad_request(exc)

    try:
        if password:
            local_auth.set_password(username, password)
        if changes:
            local_auth.update_user(username, **changes)
    except local_auth.LocalAuthError as exc:
        raise _bad_request(exc)
    current = local_auth.get_user_public(username)
    detail: Dict[str, Any] = {"username": username, **changes}
    if password:
        detail["password"] = "changed"
    audit.log_event("user.update", _actor(request), detail=detail)
    return current


def _guard_last_admin(existing: Dict[str, Any], changes: Dict[str, Any]) -> None:
    """Refuse a change that would leave no enabled administrator.

    Checked *before* writing so a rejected request never leaves the store in a
    half-applied state (the console would otherwise lock itself out).
    """
    was_active_admin = bool(existing.get("admin")) and not existing.get("disabled")
    if not was_active_admin:
        return
    still_admin = changes.get("admin", existing.get("admin"))
    still_enabled = not changes.get("disabled", existing.get("disabled"))
    if still_admin and still_enabled:
        return
    if local_auth.admin_count() <= 1:
        raise HTTPException(
            status_code=409,
            detail="the last enabled administrator cannot be demoted or disabled",
        )


@router.delete("/users/{username}")
def delete_user(username: str, request: Request):
    _require_local_auth()
    record = local_auth.get_user(username)
    if record is None:
        raise HTTPException(status_code=404, detail="user not found")
    # Only an *enabled* admin counts towards the guard: deleting an already
    # disabled one cannot lock anybody out.
    if (
        record.get("admin")
        and not record.get("disabled")
        and local_auth.admin_count() <= 1
    ):
        raise HTTPException(
            status_code=409, detail="the last enabled administrator cannot be deleted"
        )
    try:
        deleted = local_auth.delete_user(username)
    except local_auth.LocalAuthError as exc:
        raise _bad_request(exc)
    audit.log_event("user.delete", _actor(request), detail={"username": username})
    return {"deleted": deleted, "username": username}


# --- chat model overrides ---------------------------------------------------
# `.env` remains the default; these endpoints change what chat generation uses
# (never embeddings). The API key is write-only: responses carry a masked hint.
# --- 对话模型覆盖配置 ------------------------------------------------------
# `.env` 仍是默认值；这些接口只改对话生成（不影响向量化/检索）。API Key 只写不读，
# 响应里仅返回掩码提示。


class ModelSettingsRequest(BaseModel):
    """Validated payload for `PUT /admin/api/model`.

    `PUT /admin/api/model` 的入参校验模型。
    """

    api_base: str = Field(min_length=1, max_length=300)
    model: str = Field(min_length=1, max_length=200)
    api_key: Optional[str] = Field(default=None, max_length=512)
    temperature: float = 0.0
    max_tokens: int = 256


@router.get("/model")
def get_model():
    """Effective settings with a masked key hint. / 返回生效配置（密钥掩码）。"""
    return model_settings.describe()


@router.put("/model")
def put_model(req: ModelSettingsRequest, request: Request):
    """Persist overrides; audited without the key. / 持久化覆盖项；审计不含密钥。"""
    try:
        saved = model_settings.save(req.model_dump())
    except ValueError as exc:
        raise _bad_request(exc)
    audit.log_event(
        "model.update",
        _actor(request),
        detail={"model": saved["model"], "api_base": saved["api_base"]},
    )
    return saved


@router.delete("/model")
def reset_model(request: Request):
    """Drop the overrides and fall back to `.env`. / 删除覆盖项，回退到 `.env`。"""
    saved = model_settings.clear()
    audit.log_event("model.reset", _actor(request), detail={"model": saved["model"]})
    return saved


class ChunkSettingsRequest(BaseModel):
    """Validated payload for `PUT /admin/api/chunking`.

    Cross-field checks (overlap vs. window) live in `chunk_settings._validate`
    so the API and direct callers share one rule set.
    """

    chunk_tokens: int = 450
    chunk_overlap_tokens: int = 50
    chinese_punctuation: bool = True
    title_prefix: bool = True


@router.get("/chunking")
def get_chunking():
    """Effective chunking settings, env defaults and signature."""
    return chunk_settings.describe()


@router.put("/chunking")
def put_chunking(req: ChunkSettingsRequest, request: Request):
    """Persist chunking overrides; the next ingestion re-chunks automatically."""
    try:
        saved = chunk_settings.save(req.model_dump())
    except ValueError as exc:
        raise _bad_request(exc)
    audit.log_event(
        "chunk.update",
        _actor(request),
        detail={"config": saved["config"], "signature": saved["signature"]},
    )
    return saved


@router.delete("/chunking")
def reset_chunking(request: Request):
    """Drop chunking overrides and fall back to `.env`."""
    saved = chunk_settings.clear()
    audit.log_event(
        "chunk.reset",
        _actor(request),
        detail={"config": saved["config"], "signature": saved["signature"]},
    )
    return saved


@router.post("/model/test")
def test_model(body: Optional[Dict[str, Any]] = None):
    """Probe the endpoint; an unsaved payload tests before saving.

    An empty body (or `{}`) tests the currently effective configuration.

    探测端点可用性；可先测未保存的候选配置。
    空 body（或 `{}`）表示测试当前生效配置。
    """
    try:
        return model_settings.probe(body or None)
    except ValueError as exc:
        raise _bad_request(exc)


# --- vector store management ------------------------------------------------
# The files card manages what is on disk; this block exposes what is actually
# indexed, including chunks whose source file is gone ("orphans").
# 文件卡片管磁盘上的文件；这里查看真正入库的向量，包括源文件已丢失的孤儿。

_DOC_ID_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_CHUNK_CHARS = 2000
_MAX_ORPHAN_ENRICH = 200


class VectorSearchRequest(BaseModel):
    dept: Optional[str] = Field(default=None, max_length=64)
    query: str = Field(min_length=1, max_length=500)
    k: int = Field(default=8, ge=1, le=20)


def _vector_catalog(dept: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """doc_id -> file description, for every supported file on disk."""
    catalog: Dict[str, Dict[str, Any]] = {}
    for item in _iter_files(dept):
        doc_id = item.get("doc_id")
        if doc_id:
            catalog[doc_id] = item
    return catalog


def _chunk_payload(doc: Any, *, max_chars: int = _MAX_CHUNK_CHARS) -> Dict[str, Any]:
    meta = doc.metadata or {}
    text = doc.page_content or ""
    payload = {
        "chunk_index": meta.get("chunk_index"),
        "doc_id": meta.get("doc_id"),
        "dept": meta.get("dept"),
        "source": meta.get("source"),
        "kind": meta.get("kind"),
        "page": meta.get("page"),
        "slide": meta.get("slide"),
        "sheet": meta.get("sheet"),
        "title": meta.get("title"),
        "doc_hash": meta.get("doc_hash"),
        "chars": len(text),
        "truncated": len(text) > max_chars,
        "text": text[:max_chars],
    }
    if "retrieval_score" in meta:
        payload["score"] = meta["retrieval_score"]
    return payload


def _vector_documents(
    catalog: Optional[Dict[str, Dict[str, Any]]] = None,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return the store summary plus existing and orphaned documents."""
    summary = kb_engine.doc_summary()
    if catalog is None:
        catalog = _vector_catalog()
    existing: List[Dict[str, Any]] = []
    for doc_id, item in catalog.items():
        existing.append(
            {
                "doc_id": doc_id,
                "dept": item.get("dept"),
                "source": item.get("source"),
                "name": item.get("name"),
                "ext": item.get("ext"),
                "size": item.get("size"),
                "mtime": item.get("mtime"),
                "chunks": int(item.get("chunks") or 0),
                "doc_hash": summary.get(doc_id, {}).get("doc_hash"),
                "status": item.get("status"),
                "exists": True,
            }
        )

    orphan_ids = sorted(set(summary) - set(catalog))
    orphans: List[Dict[str, Any]] = []
    for index, doc_id in enumerate(orphan_ids):
        meta: Dict[str, Any] = {}
        if index < _MAX_ORPHAN_ENRICH:
            chunks = kb_engine.fetch_doc_chunks(doc_id, offset=0, limit=1)
            if chunks:
                meta = chunks[0].metadata or {}
        entry = summary.get(doc_id) or {}
        orphans.append(
            {
                "doc_id": doc_id,
                "dept": meta.get("dept"),
                "source": meta.get("source"),
                "name": None,
                "ext": None,
                "size": None,
                "mtime": None,
                "chunks": int(entry.get("chunks") or 0),
                "doc_hash": entry.get("doc_hash"),
                "status": "orphan",
                "exists": False,
            }
        )
    documents = existing + orphans
    documents.sort(key=lambda item: (item.get("dept") or "", item.get("source") or "", item["doc_id"]))
    return summary, documents


@router.get("/vectors")
def list_vectors(dept: Optional[str] = None, status: Optional[str] = None):
    """Vector stats plus per-document chunk counts.

    向量统计与按文档的 chunk 数；`status` 可筛 indexed/changed/pending/orphan。
    """
    if dept:
        dept = validate_dept(dept)
    if status:
        status = status.strip().lower()
        if status not in {"all", "indexed", "changed", "pending", "orphan"}:
            raise HTTPException(status_code=400, detail="invalid vector status")

    catalog = _vector_catalog()
    summary, documents = _vector_documents(catalog)
    info = kb_engine.vector_info()
    stats: Dict[str, Any] = {
        "backend": info.get("backend"),
        "collection": info.get("collection"),
        "chunks": int(kb_engine.count()),
        "live_chunks": int(kb_engine.live_count()),
        "documents": len(summary),
        "orphans": len(set(summary) - set(catalog)),
        "depts": sorted(kb_engine.list_depts()),
        "dimension": info.get("dimension"),
        "index_type": info.get("index_type"),
        "sparse_ready": info.get("sparse_ready"),
        "hybrid": info.get("hybrid"),
        "rebuilding": info.get("rebuilding"),
    }
    if dept:
        documents = [item for item in documents if item.get("dept") == dept]
    if status and status != "all":
        documents = [item for item in documents if item.get("status") == status]
    return {"stats": stats, "documents": documents}


@router.get("/vectors/chunks")
def list_vector_chunks(doc_id: str, offset: int = 0, limit: int = 50):
    """Chunk metadata and text for one document.

    单个文档的 chunk 元数据与正文（正文按 2000 字截断）。
    """
    if not _DOC_ID_RE.match(doc_id or ""):
        raise HTTPException(status_code=400, detail="invalid doc_id")
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 50), 200))
    total = int(kb_engine.count_by_doc_id(doc_id))
    docs = kb_engine.fetch_doc_chunks(doc_id, offset=offset, limit=limit)
    return {
        "doc_id": doc_id,
        "total": total,
        "offset": offset,
        "limit": limit,
        "chunks": [_chunk_payload(doc) for doc in docs],
    }


@router.post("/vectors/search")
def search_vectors(req: VectorSearchRequest):
    """Run a retrieval query and return the scored chunks.

    执行一次检索并返回带分数的 chunk，用于核对召回。
    """
    depts: Any
    if req.dept:
        dept = validate_dept(req.dept)
        if dept not in kb_engine.list_depts():
            raise HTTPException(status_code=404, detail="department not found")
        depts = dept
    else:
        depts = sorted(kb_engine.list_depts())
    if not depts:
        return {"query": req.query, "depts": [], "k": req.k, "results": []}
    docs = kb_engine.search(req.query, depts, req.k)
    return {
        "query": req.query,
        "depts": depts,
        "k": req.k,
        "results": [_chunk_payload(doc, max_chars=600) for doc in docs],
    }


@router.delete("/vectors/{doc_id}")
def delete_vectors(doc_id: str, request: Request):
    """Delete every chunk of one document, leaving the file on disk.

    只删向量不删文件；再次增量入库会重新写入。
    """
    if not _DOC_ID_RE.match(doc_id or ""):
        raise HTTPException(status_code=400, detail="invalid doc_id")
    if _ingest_lock.locked():
        raise HTTPException(status_code=409, detail="an ingestion job is already running")
    removed = int(kb_engine.delete_by_doc_id(doc_id))
    audit.log_event(
        "vector.delete",
        _actor(request),
        detail={"doc_id": doc_id, "chunks_removed": removed},
    )
    return {"doc_id": doc_id, "chunks_removed": removed}


@router.post("/vectors/prune")
def prune_vectors(request: Request):
    """Delete orphan chunks whose source file is no longer on disk.

    清理源文件已不存在的孤儿向量。
    """
    if _ingest_lock.locked():
        raise HTTPException(status_code=409, detail="an ingestion job is already running")
    summary = kb_engine.doc_summary()
    catalog = _vector_catalog()
    orphan_ids = sorted(set(summary) - set(catalog))
    removed = int(kb_engine.delete_by_doc_ids(orphan_ids)) if orphan_ids else 0
    audit.log_event(
        "vector.prune",
        _actor(request),
        detail={"documents_removed": len(orphan_ids), "chunks_removed": removed},
    )
    return {"documents_removed": len(orphan_ids), "chunks_removed": removed}
