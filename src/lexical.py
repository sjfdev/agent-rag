"""Lexical (BM25) retrieval used for hybrid search.

The vector store is the source of truth; this module builds an in-memory BM25
index from the chunks that belong to one department. The index is cached and
invalidated through a version counter that the engine bumps on any write, so a
stale index is never served after ingestion.
"""

import re
import threading
from typing import Dict, List, Tuple

from langchain_core.documents import Document

_CJK = r"\u4e00-\u9fff"
_LATIN = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(f"[{_CJK}]+")


def tokenize(text: str) -> List[str]:
    """Tokenize for both English and Chinese.

    Latin text is split on word boundaries. CJK runs contribute unigrams and
    bigrams, which gives reasonable recall without a segmenter.
    """
    lowered = (text or "").lower()
    tokens = _LATIN.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _key(doc: Document) -> Tuple:
    meta = doc.metadata or {}
    return (
        meta.get("doc_id"),
        meta.get("chunk_index"),
        meta.get("source"),
        meta.get("page"),
        meta.get("slide"),
        meta.get("sheet"),
    )


class LexicalIndex:
    """BM25 index over a fixed list of Documents."""

    def __init__(self, docs: List[Document]):
        self.docs = docs
        self._bm25 = None
        if docs:
            from rank_bm25 import BM25Okapi

            corpus = [tokenize(doc.page_content) for doc in docs]
            # BM25Okapi divides by average length; guard empty token lists.
            if any(corpus):
                self._bm25 = BM25Okapi(corpus)

    def search(self, query: str, k: int) -> List[Document]:
        if self._bm25 is None or not self.docs:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            zip(scores, self.docs), key=lambda pair: float(pair[0]), reverse=True
        )
        return [doc for score, doc in ranked[:k] if score > 0]


class LexicalCache:
    """Department -> BM25 index, keyed by the engine's write version."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._version = None
        self._indexes: Dict[str, LexicalIndex] = {}

    def get(
        self, dept: str, version: int, loader, max_docs: int = 0
    ) -> LexicalIndex:
        with self._lock:
            if self._version != version:
                self._indexes.clear()
                self._version = version
            cached = self._indexes.get(dept)
            if cached is not None:
                return cached

        # Build outside the lock: loading chunks can be slow and callers for
        # other departments should not block on it.
        docs = loader(dept)
        if max_docs and len(docs) > max_docs:
            # Avoid unbounded memory use on very large departments.
            docs = docs[:max_docs]
        index = LexicalIndex(docs)
        with self._lock:
            if self._version != version:
                # A write landed while we were building; do not cache an index
                # that predates it. The next call rebuilds against the new data.
                return index
            return self._indexes.setdefault(dept, index)

    def invalidate(self) -> None:
        with self._lock:
            self._indexes.clear()
            self._version = None


def rrf_fuse(
    ranked_lists: List[List[Document]], limit: int, k: int = 60
) -> List[Document]:
    """Reciprocal-rank fusion of several ranked Document lists."""
    scores: Dict[Tuple, float] = {}
    by_key: Dict[Tuple, Document] = {}
    for docs in ranked_lists:
        for rank, doc in enumerate(docs, start=1):
            key = _key(doc)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            by_key.setdefault(key, doc)
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [by_key[key] for key, _ in ordered[:limit]]
