"""Read-only source preview for chat citations.

The chat answer cites `source` plus an optional page/slide/sheet. The UI can
expand a citation into the indexed text behind it without touching the original
file, so the preview works for every supported format and for both vector
backends. Identity follows `/chat`: a bearer token wins in jwt mode; in
`AUTH_MODE=none` the caller supplies `user_id`/`user_dept` in the query string.
Department filtering happens inside the vector store and the document ACL is
applied afterwards, so a preview can never expose another department's text.
"""

import logging
import mimetypes
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from src import acl, auth
from src.api_access import require_chat_key
from src.config import settings
from src.engine import kb_engine
from src.validation import InvalidDepartment, validate_depts

logger = logging.getLogger("agent-rag.sources")

router = APIRouter(dependencies=[Depends(require_chat_key)])

MAX_PREVIEW_CHUNKS = 12
MAX_PREVIEW_CHARS = 20000


def _caller(
    headers,
    user_id: str,
    user_dept: Optional[str],
    user_depts: List[str],
    user_groups: List[str],
) -> Tuple[str, List[str], List[str]]:
    try:
        identity = auth.identity_from_headers(headers)
    except auth.AuthError as exc:
        logger.warning("rejected unauthenticated preview: %s", exc)
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "invalid or missing token"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if identity is not None:
        return identity.user_id, list(identity.departments), list(identity.groups)
    depts = list(user_depts or [])
    if user_dept:
        depts.insert(0, user_dept)
    return (
        (user_id or "").strip(),
        list(dict.fromkeys(dept for dept in depts if dept)),
        list(user_groups or []),
    )


def _safe_depts(depts: List[str]) -> List[str]:
    try:
        return validate_depts(depts)
    except InvalidDepartment:
        if not depts:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_department",
                    "message": (
                        "the identity has no department; add one to the account "
                        "or set AUTH_DEFAULT_DEPTS"
                    ),
                },
            )
        raise HTTPException(
            status_code=400, detail={"error": "invalid_department"}
        )


def _truncate(docs) -> Tuple[List[dict], bool]:
    chunks: List[dict] = []
    remaining = MAX_PREVIEW_CHARS
    truncated = len(docs) > MAX_PREVIEW_CHUNKS
    for doc in docs[:MAX_PREVIEW_CHUNKS]:
        text = doc.page_content or ""
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        if text:
            meta = doc.metadata or {}
            chunks.append(
                {"text": text, "chunk_index": int(meta.get("chunk_index") or 0)}
            )
            remaining -= len(text)
        if remaining <= 0:
            truncated = True
            break
    return chunks, truncated


def _source_file(source: str) -> Path:
    """Resolve a stored `source` under DATA_DIR; reject traversal."""
    root = Path(settings.DATA_DIR).expanduser().resolve()
    candidate = (root / source).resolve()
    if candidate == root or root not in candidate.parents:
        raise HTTPException(status_code=400, detail={"error": "invalid_source_path"})
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail={"error": "source_file_not_found"})
    return candidate


@router.get("/sources/content")
def source_content(
    request: Request,
    source: str = Query(min_length=1, max_length=512),
    user_id: str = Query(default="", max_length=64),
    user_dept: Optional[str] = Query(default=None, max_length=64),
    user_depts: List[str] = Query(default=[]),
    user_groups: List[str] = Query(default=[]),
    page: Optional[int] = Query(default=None, ge=0),
    slide: Optional[int] = Query(default=None, ge=0),
    sheet: Optional[str] = Query(default=None, max_length=128),
):
    caller, depts, groups = _caller(
        request.headers, user_id, user_dept, user_depts, user_groups
    )
    safe_depts = _safe_depts(depts)

    docs = kb_engine.fetch_source_chunks(
        source,
        safe_depts,
        page=page,
        slide=slide,
        sheet=sheet,
        limit=MAX_PREVIEW_CHUNKS + 1,
    )
    if docs and settings.ACL_ENABLED:
        docs = acl.filter_documents(docs, caller, groups)
    if not docs:
        raise HTTPException(status_code=404, detail={"error": "source_not_found"})

    meta = docs[0].metadata or {}
    chunks, truncated = _truncate(docs)
    if not chunks:
        raise HTTPException(status_code=404, detail={"error": "source_not_found"})
    return {
        "source": meta.get("source") or source,
        "dept": meta.get("dept") or "",
        "kind": meta.get("kind") or "",
        "title": meta.get("title") or "",
        "page": meta.get("page") or None,
        "slide": meta.get("slide") or None,
        "sheet": meta.get("sheet") or None,
        "chunks": chunks,
        "truncated": truncated,
    }


@router.get("/sources/file")
def source_file(
    request: Request,
    source: str = Query(min_length=1, max_length=512),
    user_id: str = Query(default="", max_length=64),
    user_dept: Optional[str] = Query(default=None, max_length=64),
    user_depts: List[str] = Query(default=[]),
    user_groups: List[str] = Query(default=[]),
    download: bool = Query(default=False),
):
    """Serve the original uploaded file after the same dept/ACL check.

    `source` comes from a citation, so authorization reuses the vector store's
    department filter and the document ACL. The path is resolved under
    `DATA_DIR` and can never escape it.
    """
    caller, depts, groups = _caller(
        request.headers, user_id, user_dept, user_depts, user_groups
    )
    safe_depts = _safe_depts(depts)
    docs = kb_engine.fetch_source_chunks(source, safe_depts, limit=1)
    if docs and settings.ACL_ENABLED:
        docs = acl.filter_documents(docs, caller, groups)
    if not docs:
        raise HTTPException(status_code=404, detail={"error": "source_not_found"})

    # Resolve the path from the authorized document metadata, not from the raw
    # query string, so the file served is exactly what the caller may read.
    authorized_source = (docs[0].metadata or {}).get("source") or source
    path = _source_file(authorized_source)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="attachment" if download else "inline",
    )
