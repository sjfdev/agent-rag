import json
import logging
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_milvus import Milvus
from langchain_openai import OpenAIEmbeddings

from src.config import settings
from src.hybrid import connection_args as _connection_args
from src.hybrid import drop_milvus_collection, resolve_hybrid_mode, version_at_least
from src.validation import validate_dept

logger = logging.getLogger(__name__)


def build_embeddings():
    """Create the embedding model based on EMBEDDING_BACKEND."""
    if settings.EMBEDDING_BACKEND == "openai":
        return OpenAIEmbeddings(
            model=settings.EMBEDDING_MODEL,
            openai_api_base=settings.LLM_API_BASE,
            openai_api_key=settings.LLM_API_KEY,
        )

    # Local sentence-transformers model: works fully offline after first download.
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(
        model_name=settings.LOCAL_EMBEDDING_MODEL,
        encode_kwargs={"normalize_embeddings": True},
    )


class BaseVectorStore(ABC):
    """Abstract base class for vector store drivers to ensure compatibility."""

    @abstractmethod
    def add_documents(self, texts: List[str], metadatas: List[Dict[str, Any]]):
        pass

    @abstractmethod
    def search(self, query: str, user_dept: str, k: int = 5) -> List[Document]:
        pass

    @abstractmethod
    def is_empty(self) -> bool:
        pass

    @abstractmethod
    def count(self) -> int:
        """Total number of indexed chunks (0 when the store is missing)."""

    @abstractmethod
    def fetch_documents(self, dept: str) -> List[Document]:
        """All chunks for one department, used to build the lexical index."""

    @abstractmethod
    def list_depts(self) -> Set[str]:
        """Departments that currently have at least one indexed chunk."""

    def hybrid_search(
        self, query: str, user_dept: str, k: int, candidates: int
    ) -> Optional[List[Document]]:
        """Server-side hybrid search, or None when the driver has no support."""
        return None

    @abstractmethod
    def get_doc_hash(self, doc_id: str) -> Optional[str]:
        """Return the stored content hash for a document id, or None if absent."""

    @abstractmethod
    def delete_by_doc_id(self, doc_id: str) -> int:
        """Delete every chunk belonging to a document. Returns chunks removed."""

    @abstractmethod
    def count_by_doc_id(self, doc_id: str) -> int:
        """Number of chunks stored for a document id."""

    @abstractmethod
    def list_doc_ids(self) -> Set[str]:
        """All document ids currently indexed."""

    @abstractmethod
    def reset(self) -> None:
        """Drop the index/collection. Used by `run_ingestion.py --reset`."""


def hits_to_documents(hits: List[Dict[str, Any]]) -> List[Document]:
    """Convert pymilvus search/hybrid_search hits into LangChain Documents."""
    docs: List[Document] = []
    for hit in hits or []:
        row = dict(hit.get("entity") or hit)
        text = row.pop("text", "")
        for key in ("id", "distance", "score", "pk"):
            row.pop(key, None)
        docs.append(Document(page_content=text, metadata=row))
    return docs


@dataclass
class MilvusCapabilities:
    """What the server/collection can do for server-side hybrid retrieval."""

    server_version: Optional[str] = None
    version_ok: bool = False
    collection_exists: bool = False
    sparse_field: bool = False
    bm25_index: bool = False
    error: Optional[str] = None

    @property
    def sparse_ready(self) -> bool:
        """True when the collection can serve BM25 sparse search right now."""
        if not self.version_ok:
            return False
        if not self.collection_exists:
            # A fresh collection will be created with a sparse field.
            return True
        return self.sparse_field and self.bm25_index


def _inspect_milvus(collection: str, sparse_field: str) -> MilvusCapabilities:
    """Probe server version and whether the collection has a BM25 sparse field."""
    caps = MilvusCapabilities()
    try:
        from pymilvus import DataType, MilvusClient

        args = _connection_args()
        client = MilvusClient(**args)
        caps.server_version = client.get_server_version()
        caps.version_ok = version_at_least(caps.server_version, "2.5.0")
        caps.collection_exists = client.has_collection(collection)
        if caps.collection_exists:
            fields = client.describe_collection(collection).get("fields", [])
            caps.sparse_field = any(
                f.get("name") == sparse_field
                and f.get("type") == DataType.SPARSE_FLOAT_VECTOR
                for f in fields
            )
            for index in client.list_indexes(collection):
                info = client.describe_index(collection, index)
                if (
                    info.get("field_name") == sparse_field
                    and str(info.get("metric_type", "")).upper() == "BM25"
                ):
                    caps.bm25_index = True
        client.close()
    except Exception as exc:  # connection issues must not stop startup
        caps.error = f"{type(exc).__name__}: {exc}"
        logger.warning("milvus capability probe failed: %s", caps.error)
    return caps


class MilvusDriver(BaseVectorStore):
    """Enterprise-scale driver using Milvus distributed cluster."""

    def __init__(self, embeddings):
        self.embeddings = embeddings
        self.collection = settings.MILVUS_COLLECTION
        connection_args = _connection_args()
        self.capabilities = _inspect_milvus(
            self.collection, settings.HYBRID_SPARSE_FIELD
        )
        self.sparse_enabled = self._decide_sparse()

        builtin_function = None
        vector_field = "vector"
        if self.sparse_enabled:
            from langchain_milvus.function import BM25BuiltInFunction

            analyzer = None
            try:
                analyzer = json.loads(settings.HYBRID_ANALYZER)
            except json.JSONDecodeError:
                logger.warning(
                    "invalid HYBRID_ANALYZER=%r, using default analyzer",
                    settings.HYBRID_ANALYZER,
                )
            builtin_function = BM25BuiltInFunction(
                output_field_names=settings.HYBRID_SPARSE_FIELD,
                analyzer_params=analyzer,
            )
            vector_field = ["vector", settings.HYBRID_SPARSE_FIELD]

        self.vector_db = Milvus(
            embedding_function=embeddings,
            connection_args=connection_args,
            collection_name=self.collection,
            auto_id=True,
            drop_old=False,
            vector_field=vector_field,
            builtin_function=builtin_function,
        )

    def _decide_sparse(self) -> bool:
        """Resolve HYBRID_ENGINE for this Milvus server; may raise on misconfig."""
        caps = self.capabilities
        choice = (settings.HYBRID_ENGINE or "auto").lower()
        if not settings.HYBRID_ENABLED or choice == "local":
            return False
        if choice == "milvus":
            if not caps.version_ok:
                raise RuntimeError(
                    "HYBRID_ENGINE=milvus requires Milvus >= 2.5, "
                    f"server reports {caps.server_version or 'unknown'}. "
                    "Either upgrade Milvus or set HYBRID_ENGINE=auto/local."
                )
            if caps.collection_exists and not caps.sparse_field:
                raise RuntimeError(
                    "HYBRID_ENGINE=milvus requires a sparse field "
                    f"'{settings.HYBRID_SPARSE_FIELD}' in collection "
                    f"'{self.collection}'. Run "
                    "`python run_ingestion.py --reset` to rebuild the schema."
                )
            if caps.collection_exists and caps.sparse_field and not caps.bm25_index:
                raise RuntimeError(
                    f"sparse field '{settings.HYBRID_SPARSE_FIELD}' has no BM25 "
                    "index. Recreate it with "
                    "`python run_ingestion.py --reset`."
                )
            return True

        # auto: use sparse only when it is genuinely usable
        if caps.sparse_ready:
            logger.info(
                "hybrid=server-side BM25 (milvus %s, collection=%s)",
                caps.server_version,
                self.collection,
            )
            return True
        reason = caps.error or (
            "server < 2.5" if not caps.version_ok else "collection lacks BM25 sparse field"
        )
        logger.info(
            "hybrid=in-process BM25 (server-side unavailable: %s); "
            "set HYBRID_ENGINE=milvus to make this fatal",
            reason,
        )
        return False

    @property
    def hybrid_mode(self) -> str:
        return "milvus" if self.sparse_enabled else "local"

    def refresh_capabilities(self) -> None:
        self.capabilities = _inspect_milvus(
            self.collection, settings.HYBRID_SPARSE_FIELD
        )

    def hybrid_search(
        self, query: str, user_dept: str, k: int, candidates: int
    ) -> Optional[List[Document]]:
        if not self.sparse_enabled:
            return None
        safe_dept = validate_dept(user_dept)
        expr = f"dept == '{safe_dept}'"
        try:
            from pymilvus import AnnSearchRequest, RRFRanker

            dense = AnnSearchRequest(
                data=[self.embeddings.embed_query(query)],
                anns_field="vector",
                param={"metric_type": "L2"},
                limit=candidates,
                expr=expr,
            )
            sparse = AnnSearchRequest(
                data=[query],
                anns_field=settings.HYBRID_SPARSE_FIELD,
                param={"metric_type": "BM25"},
                limit=candidates,
                expr=expr,
            )
            results = self.vector_db.client.hybrid_search(
                collection_name=self.collection,
                reqs=[dense, sparse],
                ranker=RRFRanker(),
                limit=k,
                output_fields=self._FETCH_FIELDS,
            )
        except Exception:
            logger.warning(
                "server-side hybrid search failed; falling back to in-process BM25",
                exc_info=True,
            )
            return None

        return hits_to_documents(results[0] if results else [])

    def _query(
        self, expr: str, fields: List[str], limit: int = 1
    ) -> List[Dict[str, Any]]:
        return self.vector_db.client.query(
            collection_name=self.collection,
            filter=expr,
            output_fields=fields,
            limit=limit,
        )

    def _count(self, expr: str, allow_missing: bool = False) -> int:
        # Milvus rejects count(*) when a pagination limit is present.
        try:
            res = self.vector_db.client.query(
                collection_name=self.collection,
                filter=expr,
                output_fields=["count(*)"],
            )
        except Exception as exc:
            # Right after a reset the collection may not exist yet; that is an
            # empty store, not a broken one. Connection errors still propagate.
            if allow_missing and "collection not found" in str(exc).lower():
                return 0
            raise
        return int(res[0]["count(*)"]) if res else 0

    def add_documents(self, texts: List[str], metadatas: List[Dict[str, Any]]):
        docs = [Document(page_content=t, metadata=m) for t, m in zip(texts, metadatas)]
        self.vector_db.add_documents(docs)
        if self.sparse_enabled:
            # The first insert may have just created the collection/BM25 index.
            self.refresh_capabilities()

    def search(self, query: str, user_dept: str, k: int = 5) -> List[Document]:
        safe_dept = validate_dept(user_dept)
        expr = f"dept == '{safe_dept}'"
        # A collection with a sparse field cannot be queried through
        # similarity_search(): Milvus refuses to return raw sparse data.
        if self.sparse_enabled or settings.HYBRID_SPARSE_FIELD in self.vector_db.fields:
            # Query the dense field explicitly.
            try:
                results = self.vector_db.client.search(
                    collection_name=self.collection,
                    data=[self.embeddings.embed_query(query)],
                    anns_field="vector",
                    search_params={"metric_type": "L2"},
                    limit=k,
                    filter=expr,
                    output_fields=self._FETCH_FIELDS,
                )
            except Exception:
                logger.warning("dense search failed", exc_info=True)
                return []
            return hits_to_documents(results[0] if results else [])
        return self.vector_db.similarity_search(query, k=k, expr=expr)

    def is_empty(self) -> bool:
        try:
            return self.count() == 0
        except Exception:
            return True

    def count(self) -> int:
        return self._count("", allow_missing=True)

    _FETCH_FIELDS = [
        "text", "source", "dept", "doc_id", "doc_hash", "kind",
        "page", "slide", "sheet", "title", "chunk_index",
    ]

    def fetch_documents(self, dept: str) -> List[Document]:
        safe_dept = validate_dept(dept)
        expr = f"dept == '{safe_dept}'"
        docs: List[Document] = []
        try:
            iterator = self.vector_db.client.query_iterator(
                collection_name=self.collection,
                batch_size=1000,
                filter=expr,
                output_fields=self._FETCH_FIELDS,
            )
            while True:
                rows = iterator.next()
                if not rows:
                    iterator.close()
                    break
                for row in rows:
                    text = row.pop("text", "")
                    docs.append(Document(page_content=text, metadata=row))
        except Exception:
            logger.warning("failed to fetch dept=%s from milvus", dept, exc_info=True)
            return []
        return docs

    def list_depts(self) -> Set[str]:
        try:
            res = self._query("", ["dept"], limit=16384)
            return {row["dept"] for row in res if row.get("dept")}
        except Exception:
            return set()

    def get_doc_hash(self, doc_id: str) -> Optional[str]:
        try:
            res = self._query(f'doc_id == "{doc_id}"', ["doc_hash"], limit=1)
            return res[0].get("doc_hash") if res else None
        except Exception:
            # Collection (or field) does not exist yet.
            return None

    def delete_by_doc_id(self, doc_id: str) -> int:
        if not doc_id:
            return 0
        try:
            res = self._query(f'doc_id == "{doc_id}"', ["pk"], limit=1)
            if not res:
                return 0
            self.vector_db.delete(expr=f'doc_id == "{doc_id}"')
            self.vector_db.client.flush(self.collection)
            return 1
        except Exception:
            logger.warning("failed to delete doc_id=%s from milvus", doc_id, exc_info=True)
            return 0

    def count_by_doc_id(self, doc_id: str) -> int:
        if not doc_id:
            return 0
        try:
            return self._count(f'doc_id == "{doc_id}"')
        except Exception:
            return 0

    def list_doc_ids(self) -> Set[str]:
        try:
            res = self._query("", ["doc_id"], limit=16384)
            return {r["doc_id"] for r in res if r.get("doc_id")}
        except Exception:
            return set()

    def reset(self) -> None:
        try:
            self.vector_db.client.drop_collection(self.collection)
            print(f"Dropped Milvus collection {self.collection}.")
        except Exception:
            logger.warning("drop collection %s failed", self.collection, exc_info=True)
        self.__init__(self.embeddings)


class FAISSDriver(BaseVectorStore):
    """Lightweight driver using local FAISS for rapid development/deployment."""

    def __init__(self, embeddings):
        self.embeddings = embeddings
        os.makedirs(settings.DB_PATH, exist_ok=True)
        # Initialize empty FAISS index if no persisted DB exists
        try:
            self.vector_db = FAISS.load_local(
                settings.DB_PATH,
                embeddings,
                allow_dangerous_deserialization=True,
            )
        except Exception:
            self.vector_db = None

    def _docs(self):
        if self.vector_db is None:
            return {}
        return self.vector_db.docstore._dict

    def add_documents(self, texts: List[str], metadatas: List[Dict[str, Any]]):
        docs = [Document(page_content=t, metadata=m) for t, m in zip(texts, metadatas)]
        if self.vector_db is None:
            self.vector_db = FAISS.from_documents(docs, self.embeddings)
        else:
            self.vector_db.add_documents(docs)
        os.makedirs(settings.DB_PATH, exist_ok=True)
        self.vector_db.save_local(settings.DB_PATH)

    def search(self, query: str, user_dept: str, k: int = 5) -> List[Document]:
        if self.vector_db is None:
            return []

        # FAISS doesn't support server-side filtering like Milvus.
        # We implement post-retrieval filtering to maintain permission control.
        # Fetch a wider candidate pool, then filter down to the user's department.
        safe_dept = validate_dept(user_dept)
        pool = max(k * 20, 50)
        results = self.vector_db.similarity_search(query, k=pool)
        filtered = [doc for doc in results if doc.metadata.get("dept") == safe_dept]
        return filtered[:k]

    def is_empty(self) -> bool:
        return self.count() == 0

    def count(self) -> int:
        if self.vector_db is None:
            return 0
        try:
            return int(self.vector_db.index.ntotal)
        except Exception:
            return 0

    def fetch_documents(self, dept: str) -> List[Document]:
        safe_dept = validate_dept(dept)
        return [
            doc
            for doc in self._docs().values()
            if doc.metadata.get("dept") == safe_dept
        ]

    def list_depts(self) -> Set[str]:
        return {
            doc.metadata["dept"]
            for doc in self._docs().values()
            if doc.metadata.get("dept")
        }

    def get_doc_hash(self, doc_id: str) -> Optional[str]:
        for doc in self._docs().values():
            if doc.metadata.get("doc_id") == doc_id:
                return doc.metadata.get("doc_hash")
        return None

    def delete_by_doc_id(self, doc_id: str) -> int:
        if not doc_id or self.vector_db is None:
            return 0
        ids = [
            key
            for key, doc in self._docs().items()
            if doc.metadata.get("doc_id") == doc_id
        ]
        if not ids:
            return 0
        self.vector_db.delete(ids)
        self.vector_db.save_local(settings.DB_PATH)
        return len(ids)

    def count_by_doc_id(self, doc_id: str) -> int:
        if not doc_id:
            return 0
        return sum(
            1 for doc in self._docs().values() if doc.metadata.get("doc_id") == doc_id
        )

    def list_doc_ids(self) -> Set[str]:
        return {
            doc.metadata["doc_id"]
            for doc in self._docs().values()
            if doc.metadata.get("doc_id")
        }

    def reset(self) -> None:
        shutil.rmtree(settings.DB_PATH, ignore_errors=True)
        self.vector_db = None
        print(f"Removed FAISS index at {settings.DB_PATH}.")


class KnowledgeBaseEngine:
    """Factory class to handle seamless switching between Milvus and FAISS."""

    def __init__(self):
        from src.lexical import LexicalCache

        self.embeddings = build_embeddings()
        # Bumped on every write so the cached BM25 indexes are rebuilt.
        self._version = 0
        self._lexical = LexicalCache()

        if settings.VECTOR_STORE_TYPE == "milvus":
            self.driver = MilvusDriver(self.embeddings)
        else:
            self.driver = FAISSDriver(self.embeddings)

    def _touch(self) -> None:
        self._version += 1
        self._lexical.invalidate()

    def add_documents(self, texts: List[str], metadatas: List[Dict[str, Any]]):
        self.driver.add_documents(texts, metadatas)
        self._touch()

    def fetch_documents(self, dept: str) -> List[Document]:
        return self.driver.fetch_documents(dept)

    def list_depts(self) -> Set[str]:
        return self.driver.list_depts()

    @property
    def hybrid_mode(self) -> str:
        if not settings.HYBRID_ENABLED:
            return "off"
        return getattr(self.driver, "hybrid_mode", "local")

    def hybrid_status(self) -> Dict[str, Any]:
        """Capability summary for /health and /info."""
        caps = getattr(self.driver, "capabilities", None)
        return {
            "enabled": settings.HYBRID_ENABLED,
            "engine": self.hybrid_mode,
            "configured": settings.HYBRID_ENGINE,
            "server_version": getattr(caps, "server_version", None),
            "sparse_field": settings.HYBRID_SPARSE_FIELD,
            "sparse_ready": bool(getattr(caps, "sparse_ready", False)),
            "sparse_error": getattr(caps, "error", None),
        }

    def warm_lexical(self, depts: Optional[Set[str]] = None) -> List[str]:
        """Build BM25 indexes up front so the first query is not slow.

        Intended to run in a background thread after startup or ingestion.
        """
        if self.hybrid_mode == "milvus":
            # The server owns the sparse index; there is nothing to build here.
            return []
        warmed = []
        for dept in sorted(depts if depts is not None else self.list_depts()):
            try:
                self.lexical_search("warmup", dept, 1)
                warmed.append(dept)
            except Exception:
                logger.warning("lexical warmup failed for dept=%s", dept, exc_info=True)
        return warmed

    def lexical_search(self, query: str, user_dept: str, k: int) -> List[Document]:
        """BM25 over the department's chunks, built on demand and cached."""
        if not query.strip() or k <= 0:
            return []
        index = self._lexical.get(
            user_dept,
            self._version,
            self.driver.fetch_documents,
            max_docs=settings.HYBRID_MAX_DOCS,
        )
        return index.search(query, k)

    def search(self, query: str, user_dept: str, k: int = 5) -> List[Document]:
        from src.lexical import rrf_fuse

        if self.hybrid_mode == "milvus":
            from src.reranker import rerank_documents

            candidates = max(k, settings.HYBRID_CANDIDATES)
            if settings.RERANK_ENABLED:
                candidates = max(candidates, settings.RERANK_CANDIDATES)
            fused = self.driver.hybrid_search(query, user_dept, candidates, candidates)
            if fused is None:
                # The server call failed; degrade for this query only.
                fused = self._local_hybrid(query, user_dept, candidates, rrf_fuse)
            if settings.RERANK_ENABLED:
                rerank_pool = max(k, settings.RERANK_CANDIDATES)
                return rerank_documents(query, fused[:rerank_pool], k)
            return fused[:k]

        if settings.HYBRID_ENABLED:
            from src.reranker import rerank_documents

            candidates = max(k, settings.HYBRID_CANDIDATES)
            if settings.RERANK_ENABLED:
                candidates = max(candidates, settings.RERANK_CANDIDATES)
            fused = self._local_hybrid(query, user_dept, candidates, rrf_fuse)
            if settings.RERANK_ENABLED:
                rerank_pool = max(k, settings.RERANK_CANDIDATES)
                return rerank_documents(query, fused[:rerank_pool], k)
            return fused[:k]

        if settings.RERANK_ENABLED:
            from src.reranker import rerank_documents

            candidates = self.driver.search(
                query, user_dept, max(k, settings.RERANK_CANDIDATES)
            )
            return rerank_documents(query, candidates, k)
        return self.driver.search(query, user_dept, k)

    def _local_hybrid(self, query: str, user_dept: str, candidates: int, fuse):
        vector_docs = self.driver.search(query, user_dept, candidates)
        lexical_docs = self.lexical_search(query, user_dept, candidates)
        return fuse([vector_docs, lexical_docs], candidates)

    def is_empty(self) -> bool:
        return self.driver.is_empty()

    def count(self) -> int:
        return self.driver.count()

    def get_doc_hash(self, doc_id: str) -> Optional[str]:
        return self.driver.get_doc_hash(doc_id)

    def delete_by_doc_id(self, doc_id: str) -> int:
        removed = self.driver.delete_by_doc_id(doc_id)
        self._touch()
        return removed

    def count_by_doc_id(self, doc_id: str) -> int:
        return self.driver.count_by_doc_id(doc_id)

    def list_doc_ids(self) -> Set[str]:
        return self.driver.list_doc_ids()

    def reset(self) -> None:
        self.driver.reset()
        self._touch()


# Singleton instance for global use
kb_engine = KnowledgeBaseEngine()
