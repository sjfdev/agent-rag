"""Session CRUD used by the chat UI and `ChatRequest.session_id`.

Identity follows the same rules as `/chat`: a bearer token wins in jwt mode;
in `AUTH_MODE=none` the caller supplies `user_id` (body on create, query string
on reads). Session ids are opaque and always checked against the caller, so a
leaked id alone does not grant access.
"""

from typing import List, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src import auth
from src.config import settings
from src.sessions import session_store

router = APIRouter()


class CreateSessionRequest(BaseModel):
    # Ignored in jwt mode; required when auth is disabled.
    user_id: str = Field(default="", max_length=64)
    dept: str = Field(default="", max_length=64)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class FeedbackRequest(BaseModel):
    seq: int = Field(ge=0)
    rating: int = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=400)


def _caller(headers, user_id: str = "") -> Tuple[str, List[str]]:
    try:
        identity = auth.identity_from_headers(headers)
    except auth.AuthError as exc:
        raise HTTPException(
            status_code=401, detail={"error": "unauthorized", "message": str(exc)}
        ) from exc
    if identity is not None:
        return identity.user_id, list(identity.departments)
    user_id = (user_id or "").strip()
    if not user_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_user_id", "message": "user_id is required"},
        )
    return user_id, []


def _enabled() -> None:
    if not settings.SESSIONS_ENABLED:
        raise HTTPException(status_code=404, detail={"error": "sessions_disabled"})


@router.post("/sessions", status_code=201)
def create_session(payload: CreateSessionRequest, request: Request):
    _enabled()
    user_id, departments = _caller(request.headers, payload.user_id)
    dept = departments[0] if departments else payload.dept.strip()
    return session_store.create_session(user_id, dept[:64])


@router.get("/sessions")
def list_sessions(
    request: Request,
    user_id: str = Query(default="", max_length=64),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    _enabled()
    caller, _ = _caller(request.headers, user_id)
    query = (q or "").strip()
    if query:
        sessions = session_store.search_sessions(caller, query, limit, offset)
    else:
        sessions = session_store.list_sessions(caller, limit, offset)
    return {"sessions": sessions, "query": query}


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    request: Request,
    user_id: str = Query(default="", max_length=64),
):
    _enabled()
    caller, _ = _caller(request.headers, user_id)
    session = session_store.get_session(session_id, caller)
    if session is None:
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})
    session["messages"] = session_store.get_messages(session_id, caller)
    return session


@router.patch("/sessions/{session_id}")
def rename_session(
    session_id: str,
    payload: RenameSessionRequest,
    request: Request,
    user_id: str = Query(default="", max_length=64),
):
    _enabled()
    caller, _ = _caller(request.headers, user_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail={"error": "empty_title"})
    if not session_store.rename_session(session_id, caller, title):
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})
    return {"renamed": True, "session_id": session_id, "title": title[:120]}


@router.post("/sessions/{session_id}/feedback")
def submit_feedback(
    session_id: str,
    payload: FeedbackRequest,
    request: Request,
    user_id: str = Query(default="", max_length=64),
):
    _enabled()
    caller, _ = _caller(request.headers, user_id)
    if payload.rating not in (1, -1):
        raise HTTPException(status_code=400, detail={"error": "invalid_rating"})
    if not session_store.set_feedback(
        session_id, caller, payload.seq, payload.rating, payload.comment
    ):
        raise HTTPException(status_code=404, detail={"error": "message_not_found"})
    return {"saved": True, "session_id": session_id, "seq": payload.seq}


@router.get("/sessions/{session_id}/feedback")
def list_feedback(
    session_id: str,
    request: Request,
    user_id: str = Query(default="", max_length=64),
):
    _enabled()
    caller, _ = _caller(request.headers, user_id)
    if session_store.get_session(session_id, caller) is None:
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})
    return {"feedback": session_store.get_feedback(session_id, caller)}


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    request: Request,
    user_id: str = Query(default="", max_length=64),
):
    _enabled()
    caller, _ = _caller(request.headers, user_id)
    if not session_store.delete_session(session_id, caller):
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})
    return {"deleted": True, "session_id": session_id}
