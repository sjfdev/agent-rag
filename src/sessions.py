"""Server-side conversation storage.

The API historically took the whole chat history in every request. That grows
linearly with the conversation and lets a client rewrite its own past. With a
`session_id` the server owns the transcript:

  * `sessions`  one row per conversation, scoped to the caller's user id
  * `messages`  ordered turns (role/content/sources/degraded)

When a conversation exceeds `2 × HISTORY_MAX_MESSAGES`, older turns are folded
into a running summary (one extra LLM call per window). Requests then carry a
summary plus the last window, so payloads stay bounded no matter how long the
chat runs.

SQLite is used because the app is single-worker by design (see AGENTS.md); the
database lives under `logs/` by default, which is already mounted and backed up
in the production compose file.
"""

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from src import metrics
from src.config import settings

logger = logging.getLogger("agent-rag.sessions")

__all__ = ["SessionStore", "session_store", "summarize_text"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    dept TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    summarized_through INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    degraded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS feedback (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    rating INTEGER NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq, user_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_feedback_session
    ON feedback(session_id, seq);
"""

_initialized: set = set()
_init_lock = threading.Lock()
# session id -> monotonic time of the last failed summary, so an unreachable
# LLM does not add its timeout to every request in that conversation.
_summary_failures: Dict[str, float] = {}
_summary_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def _like_pattern(value: str) -> str:
    """Escape LIKE wildcards so a search for `%` matches a literal percent."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class SessionStore:
    """Small SQLite-backed store; one connection per operation for thread safety."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path

    @property
    def path(self) -> Path:
        return _resolve(self._db_path or settings.SESSIONS_DB_PATH)

    def _ensure_schema(self, path: Path) -> None:
        key = str(path)
        if key in _initialized:
            return
        with _init_lock:
            if key in _initialized:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=10)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            _initialized.add(key)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        path = self.path
        self._ensure_schema(path)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    # -- sessions ---------------------------------------------------------

    def create_session(self, user_id: str, dept: str = "") -> Dict[str, Any]:
        import uuid

        session_id = uuid.uuid4().hex
        now = _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO sessions (id, user_id, dept, title, summary, "
                "summarized_through, created_at, updated_at) "
                "VALUES (?, ?, ?, '', '', 0, ?, ?)",
                (session_id, user_id, dept, now, now),
            )
            conn.commit()
        return {
            "session_id": session_id,
            "title": "",
            "dept": dept,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }

    def get_session(self, session_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Return a session only when it belongs to `user_id`."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT s.*, COALESCE((SELECT MAX(m.seq) FROM messages m "
                "WHERE m.session_id = s.id), 0) AS message_count "
                "FROM sessions s WHERE s.id = ? AND s.user_id = ?",
                (session_id, user_id),
            ).fetchone()
        return self._decode_session(row) if row else None

    def list_sessions(
        self, user_id: str, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT s.*, COALESCE((SELECT MAX(m.seq) FROM messages m "
                "WHERE m.session_id = s.id), 0) AS message_count "
                "FROM sessions s WHERE s.user_id = ? "
                "ORDER BY s.updated_at DESC LIMIT ? OFFSET ?",
                (user_id, max(int(limit), 1), max(int(offset), 0)),
            ).fetchall()
        return [self._decode_session(row) for row in rows]

    def search_sessions(
        self,
        user_id: str,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Sessions whose title, summary or any message matches `query`."""
        pattern = _like_pattern((query or "").strip())
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT s.*, COALESCE((SELECT MAX(m.seq) FROM messages m "
                "WHERE m.session_id = s.id), 0) AS message_count "
                "FROM sessions s WHERE s.user_id = ? AND ("
                "s.id LIKE ? ESCAPE '\\' OR s.title LIKE ? ESCAPE '\\' "
                "OR s.summary LIKE ? ESCAPE '\\' "
                "OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id "
                "AND m.content LIKE ? ESCAPE '\\')) "
                "ORDER BY s.updated_at DESC LIMIT ? OFFSET ?",
                (
                    user_id,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    max(int(limit), 1),
                    max(int(offset), 0),
                ),
            ).fetchall()
        return [self._decode_session(row) for row in rows]

    def rename_session(self, session_id: str, user_id: str, title: str) -> bool:
        cleaned = " ".join((title or "").split())[:120]
        if not cleaned:
            return False
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (cleaned, _now(), session_id, user_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def delete_session(self, session_id: str, user_id: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    # -- messages ---------------------------------------------------------

    def append_turn(
        self,
        session_id: str,
        user_id: str,
        question: str,
        answer: str,
        sources: Optional[List[Dict[str, Any]]] = None,
        degraded: bool = False,
        return_seq: bool = False,
    ) -> Any:
        """Append a user/assistant pair.

        Returns False for a foreign session. When `return_seq` is true it
        returns the assistant message sequence instead (or None on failure), so
        the caller can attach feedback to that turn.
        """
        now = _now()
        payload = json.dumps(sources or [], ensure_ascii=False)
        with self._connection() as conn:
            session = conn.execute(
                "SELECT id, title FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if session is None:
                return None if return_seq else False
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            conn.executemany(
                "INSERT INTO messages (session_id, seq, role, content, sources, "
                "degraded, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (session_id, next_seq, "user", question, "[]", 0, now),
                    (session_id, next_seq + 1, "assistant", answer, payload, int(degraded), now),
                ],
            )
            title = session["title"]
            if not title:
                title = " ".join(question.split())[:60]
            conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, session_id),
            )
            conn.commit()
        return next_seq + 1 if return_seq else True

    def get_messages(
        self, session_id: str, user_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone():
                return []
            columns = (
                "m.seq, m.role, m.content, m.sources, m.degraded, m.created_at, "
                "f.rating AS feedback_rating, f.comment AS feedback_comment"
            )
            join = (
                "FROM messages m LEFT JOIN feedback f ON f.session_id = m.session_id "
                "AND f.seq = m.seq AND f.user_id = ?"
            )
            sql = f"SELECT {columns} {join} WHERE m.session_id = ? ORDER BY m.seq"
            params: Tuple[Any, ...] = (user_id, session_id)
            if limit:
                sql = (
                    f"SELECT * FROM (SELECT {columns} {join} "
                    "WHERE m.session_id = ? ORDER BY m.seq DESC LIMIT ?) "
                    "ORDER BY seq"
                )
                params = (user_id, session_id, max(int(limit), 1))
            rows = conn.execute(sql, params).fetchall()
        return [self._decode_row(row) for row in rows]

    # -- feedback ---------------------------------------------------------

    def set_feedback(
        self,
        session_id: str,
        user_id: str,
        seq: int,
        rating: int,
        comment: str = "",
    ) -> bool:
        """Store one rating per (session, message, user); returns False if invalid."""
        if rating not in (1, -1):
            return False
        with self._connection() as conn:
            owned = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if owned is None:
                return False
            message = conn.execute(
                "SELECT role FROM messages WHERE session_id = ? AND seq = ?",
                (session_id, int(seq)),
            ).fetchone()
            if message is None or message["role"] != "assistant":
                return False
            conn.execute(
                "INSERT INTO feedback (session_id, seq, user_id, rating, comment, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, seq, user_id) DO UPDATE SET "
                "rating = excluded.rating, comment = excluded.comment, "
                "created_at = excluded.created_at",
                (session_id, int(seq), user_id, int(rating), comment[:400], _now()),
            )
            conn.commit()
        return True

    def get_feedback(
        self, session_id: str, user_id: str
    ) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT seq, rating, comment, created_at FROM feedback "
                "WHERE session_id = ? AND user_id = ? ORDER BY seq",
                (session_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def context(
        self, session_id: str, user_id: str
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Return (summary, recent messages) for building model input.

        Only messages after `summarized_through` are loaded, then trimmed to the
        last `HISTORY_MAX_MESSAGES` so an accidental summary failure still
        cannot make the prompt grow without bound.
        """
        window = max(int(settings.HISTORY_MAX_MESSAGES), 1)
        with self._connection() as conn:
            session = conn.execute(
                "SELECT summary, summarized_through FROM sessions "
                "WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if session is None:
                return "", []
            rows = conn.execute(
                "SELECT seq, role, content, sources, degraded, created_at "
                "FROM messages WHERE session_id = ? AND seq > ? "
                "ORDER BY seq",
                (session_id, int(session["summarized_through"])),
            ).fetchall()
        messages = [self._decode_row(row) for row in rows][-window:]
        return session["summary"] or "", messages

    @staticmethod
    def _decode_session(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["session_id"] = item.pop("id")
        return item

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        try:
            item["sources"] = json.loads(item.get("sources") or "[]")
        except (TypeError, json.JSONDecodeError):
            item["sources"] = []
        item["degraded"] = bool(item.get("degraded"))
        rating = item.pop("feedback_rating", None)
        comment = item.pop("feedback_comment", "") or ""
        item["feedback"] = (
            {"rating": int(rating), "comment": comment} if rating is not None else None
        )
        return item

    # -- summary ----------------------------------------------------------

    def maybe_summarize(self, session_id: str, user_id: str) -> None:
        """Fold older turns into a running summary once the window overflows.

        Summaries are generated for a whole extra window at once, so a long
        chat costs one extra LLM call per window, not per turn. Any failure is
        logged and leaves the DB unchanged; chat continues with a trimmed
        window.
        """
        if not settings.SESSION_SUMMARY_ENABLED:
            return
        retry_after = max(int(settings.SESSION_SUMMARY_RETRY_SECONDS), 0)
        if retry_after:
            with _summary_lock:
                failed_at = _summary_failures.get(session_id)
                if failed_at is not None and time.monotonic() - failed_at < retry_after:
                    return
        window = max(int(settings.HISTORY_MAX_MESSAGES), 1)
        with self._connection() as conn:
            session = conn.execute(
                "SELECT summary, summarized_through FROM sessions "
                "WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if session is None:
                return
            through = int(session["summarized_through"])
            total = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            unsummarized = int(total) - through
            if unsummarized <= 2 * window:
                return
            to_take = unsummarized - window
            rows = conn.execute(
                "SELECT seq, role, content FROM messages "
                "WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (session_id, through, to_take),
            ).fetchall()
        if not rows:
            return
        target = int(rows[-1]["seq"])
        previous = session["summary"] or ""
        started = time.perf_counter()
        try:
            summary = summarize_text(previous, [dict(row) for row in rows])
        except Exception:
            metrics.observe_session_summary("error", time.perf_counter() - started)
            with _summary_lock:
                _summary_failures[session_id] = time.monotonic()
            logger.warning(
                "session summary failed id=%s turns=%d", session_id, len(rows), exc_info=True
            )
            return
        budget = max(int(settings.SESSION_SUMMARY_MAX_CHARS), 100)
        summary = summary.strip()[:budget]
        if not summary:
            metrics.observe_session_summary("error", time.perf_counter() - started)
            with _summary_lock:
                _summary_failures[session_id] = time.monotonic()
            return
        with self._connection() as conn:
            conn.execute(
                "UPDATE sessions SET summary = ?, summarized_through = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (summary, target, _now(), session_id, user_id),
            )
            conn.commit()
        with _summary_lock:
            _summary_failures.pop(session_id, None)
        metrics.observe_session_summary("ok", time.perf_counter() - started)


_SUMMARY_PROMPT = (
    "You maintain a compact running summary of a support chat. Merge the "
    "previous summary and the new turns into at most 120 words. Keep concrete "
    "facts, names, numbers and decisions. Do not invent anything. Reply with "
    "the summary only."
)


def summarize_text(previous: str, messages: List[Dict[str, Any]]) -> str:
    """One extra LLM call used by `maybe_summarize`; patchable in tests."""
    import requests

    base = (settings.LLM_API_BASE or "").rstrip("/")
    if not base:
        raise RuntimeError("LLM_API_BASE is not configured")
    lines = []
    for item in messages:
        content = " ".join(str(item.get("content") or "").split())[:500]
        if content:
            lines.append(f"{item.get('role', 'user')}: {content}")
    prompt = "Previous summary:\n" + (previous or "(none)") + "\n\nNew turns:\n" + "\n".join(lines)
    response = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
        json={
            "model": settings.LLM_MODEL,
            "messages": [
                {"role": "system", "content": _SUMMARY_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": settings.SESSION_SUMMARY_MAX_TOKENS,
            "temperature": 0,
        },
        timeout=min(max(int(settings.LLM_TIMEOUT), 1), 15),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"summary endpoint returned HTTP {response.status_code}")
    data = response.json()
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if not text.strip():
        raise RuntimeError("summary endpoint returned an empty answer")
    return text


session_store = SessionStore()
