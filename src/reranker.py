"""Optional cross-encoder reranking for first-stage retrieval candidates."""

import threading
from typing import List, Optional

from langchain_core.documents import Document

from src.config import settings


class EnterpriseReranker:
    """Lazily load a CrossEncoder and rerank (query, document) pairs.

    Loading is deferred until the first request so that importing the engine
    stays cheap, and any load/inference failure degrades to the original order
    instead of breaking retrieval.
    """

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or settings.RERANK_MODEL
        self._model = None
        self._failed = False
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is not None or self._failed:
            return self._model
        with self._lock:
            if self._model is None and not self._failed:
                try:
                    from sentence_transformers import CrossEncoder

                    self._model = CrossEncoder(self.model_name)
                except Exception as exc:  # missing cache, offline, OOM...
                    self._failed = True
                    print(f"[reranker] disabled, cannot load {self.model_name}: {exc}")
        return self._model

    def rerank(self, query: str, docs: List[Document], top_k: int = 3) -> List[Document]:
        if len(docs) <= 1:
            return docs[:top_k]
        model = self._ensure_model()
        if model is None:
            return docs[:top_k]
        try:
            pairs = [[query, doc.page_content] for doc in docs]
            scores = model.predict(pairs, batch_size=8)
        except Exception as exc:
            print(f"[reranker] inference failed, keeping vector order: {exc}")
            return docs[:top_k]
        ranked = sorted(zip(scores, docs, strict=False), key=lambda pair: float(pair[0]), reverse=True)
        return [doc for _, doc in ranked[:top_k]]


_reranker: Optional[EnterpriseReranker] = None
_reranker_lock = threading.Lock()


def get_reranker() -> EnterpriseReranker:
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                _reranker = EnterpriseReranker()
    return _reranker


def rerank_documents(query: str, docs: List[Document], top_k: int) -> List[Document]:
    if not settings.RERANK_ENABLED:
        return docs[:top_k]
    return get_reranker().rerank(query, docs, top_k)
