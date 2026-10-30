"""Answer cache for repeated questions (FAQ cache).

Keyed by department + a normalised question, so the same question in another
department never shares an entry. Entries are dropped when the knowledge base
changes (ingestion, deletion, rebuild) and expire after a TTL.

Only answers that are safe to replay are stored: a single LLM call, no tool
use, no error. That keeps behaviour identical to a fresh request.
"""

import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from src.config import settings

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize_question(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    cleaned = _PUNCT.sub(" ", (text or "").lower())
    return _WHITESPACE.sub(" ", cleaned).strip()


class AnswerCache:
    """Small LRU + TTL cache guarded by a lock (safe for the threadpool)."""

    def __init__(self, max_entries: int = 512, ttl_seconds: float = 86400.0):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: "OrderedDict[Tuple[str, str], Tuple[float, Dict[str, Any]]]" = (
            OrderedDict()
        )
        self._hits = 0
        self._misses = 0

    @staticmethod
    def key(dept: str, question: str) -> Tuple[str, str]:
        return (dept, normalize_question(question))

    def get(self, dept: str, question: str) -> Optional[Dict[str, Any]]:
        key = self.key(dept, question)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            created, payload = entry
            if now - created > self.ttl_seconds:
                del self._entries[key]
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return dict(payload)

    def put(self, dept: str, question: str, payload: Dict[str, Any]) -> None:
        key = self.key(dept, question)
        with self._lock:
            self._entries[key] = (time.monotonic(), dict(payload))
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
            }


# Process-wide cache; cleared on any knowledge base write.
answer_cache = AnswerCache(
    max_entries=settings.FAQ_CACHE_MAX_ENTRIES,
    ttl_seconds=float(settings.FAQ_CACHE_TTL),
)
