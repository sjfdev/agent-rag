"""Query-embedding cache.

Wraps a LangChain ``Embeddings`` object and memoises ``embed_query`` results.
Ingestion (``embed_documents``) is never cached: it runs once per file and the
memory would be wasted. The cache is process-local and bounded.
"""

import threading
from collections import OrderedDict
from typing import Any, List

from langchain_core.embeddings import Embeddings

from src.config import settings


class CachedEmbeddings(Embeddings):
    """Embeddings proxy that caches query vectors by (model, text)."""

    def __init__(self, inner: Embeddings, namespace: str, max_entries: int = 2048):
        self._inner = inner
        self._namespace = namespace
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, List[float]]" = OrderedDict()
        self._hits = 0
        self._misses = 0

    def __getattr__(self, item: str) -> Any:
        # Expose model_name / client / etc. so wrappers and probes still work.
        return getattr(self._inner, item)

    def _key(self, text: str) -> str:
        return f"{self._namespace}\x00{text}"

    def _observe(self, result: str) -> None:
        try:
            from src.metrics import observe_cache

            observe_cache("embedding", result)
        except Exception:  # metrics must never break embeddings
            pass

    def embed_query(self, text: str) -> List[float]:
        key = self._key(text)
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                self._hits += 1
                self._observe("hit")
                return list(cached)
            self._misses += 1
            self._observe("miss")

        vector = self._inner.embed_query(text)
        with self._lock:
            self._entries[key] = list(vector)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return vector

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # Ingestion path: pass through, never cache (large and one-shot).
        return self._inner.embed_documents(texts)

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "namespace": self._namespace,
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def cache_namespace() -> str:
    """Identity of the embedding model so a swap never reuses stale vectors."""
    if settings.EMBEDDING_BACKEND == "openai":
        return f"openai:{settings.EMBEDDING_MODEL}"
    if settings.EMBEDDING_BACKEND == "fake":
        return "fake"
    return f"local:{settings.LOCAL_EMBEDDING_MODEL}"
