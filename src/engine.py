import hashlib
import json
import logging
import os
import random
import shutil
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_milvus import Milvus
from langchain_openai import OpenAIEmbeddings

from src.config import settings
from src.hybrid import connection_args as _connection_args
from src.hybrid import version_at_least
from src.validation import validate_dept

logger = logging.getLogger(__name__)


class FakeEmbeddings:
    """Deterministic, dependency-free embeddings for offline tests.

    Enabled with ``EMBEDDING_BACKEND=fake``. Vectors depend only on the text, so
    tests are reproducible and never download a model or call a network API.
    """

    def __init__(self, dim: int = 64):
        self.dim = dim

    def _vector(self, text: str):
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        values = [rng.uniform(-1.0, 1.0) for _ in range(self.dim)]
        norm = sum(v * v for v in values) ** 0.5 or 1.0
        return [v / norm for v in values]

    def embed_query(self, text: str):
        return self._vector(text)

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]


def _raw_embeddings():
    if settings.EMBEDDING_BACKEND == "fake":
        return FakeEmbeddings()
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


def build_embeddings():
    """Create the embedding model, caching query vectors in memory.

    Namespaced by (backend, model) so changing the model never reuses vectors
    produced by a different one.
    """
    from src.embedding_cache import CachedEmbeddings, cache_namespace

    raw = _raw_embeddings()
    if settings.EMBEDDING_CACHE_MAX_ENTRIES <= 0:
        return raw
    return CachedEmbeddings(
        raw,
        namespace=cache_namespace(),
        max_entries=settings.EMBEDDING_CACHE_MAX_ENTRIES,
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
        """Chunks in the active write target (shadow during a rebuild)."""

    def count_live(self) -> int:
        """Chunks in the collection that queries are currently served from."""
        return self.count()

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

    def delete_by_doc_ids(self, doc_ids: List[str]) -> int:
        """Delete several documents at once. Returns chunks removed.

        Drivers override this to issue one round-trip per batch; the default
        keeps correctness for drivers that only support single-doc deletes.
        """
        return sum(self.delete_by_doc_id(doc_id) for doc_id in doc_ids)

    @abstractmethod
    def doc_summary(self) -> Dict[str, Dict[str, Any]]:
        """Map doc_id -> {doc_hash, chunks} without loading chunk text."""

    def delete_stale_versions(self, doc_id: str, keep_hash: str) -> int:
        """Delete chunks of `doc_id` whose doc_hash != keep_hash.

        Used by the safe-replace flow: write the new version first, then remove
        the old one, so a crash never leaves the document missing.
        """
        return 0

    def begin_rebuild(self) -> None:
        """Start writing into a shadow index; reads keep serving the live one."""

    def commit_rebuild(self) -> None:
        """Swap the shadow index in and drop the old one."""

    def abort_rebuild(self) -> None:
        """Discard the shadow index and keep serving the live one."""

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

    @property
    def _write_db(self):
        """Store writes go to: the shadow collection during a rebuild."""
        return getattr(self, "_rebuild_target", None) or self.vector_db

    @property
    def _write_collection(self) -> str:
        if getattr(self, "_rebuild_target", None) is not None:
            return self.shadow_collection
        return self.collection

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
        docs = [Document(page_content=t, metadata=m) for t, m in zip(texts, metadatas, strict=False)]
        self._write_db.add_documents(docs)
        if self.sparse_enabled and getattr(self, "_rebuild_target", None) is None:
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
        if getattr(self, "_rebuild_target", None) is not None:
            return self._count_in(self.shadow_collection, "")
        return self._count("", allow_missing=True)

    def count_live(self) -> int:
        return self._count("", allow_missing=True)

    def _count_in(self, collection: str, expr: str) -> int:
        client = self.vector_db.client
        try:
            if not client.has_collection(collection):
                return 0
            res = client.query(
                collection_name=collection,
                filter=expr,
                output_fields=["count(*)"],
            )
            return int(res[0]["count(*)"]) if res else 0
        except Exception:
            return 0

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

    def _query_in(self, collection: str, expr: str, fields: List[str], limit: int):
        client = self.vector_db.client
        if not client.has_collection(collection):
            return []
        return client.query(
            collection_name=collection,
            filter=expr,
            output_fields=fields,
            limit=limit,
        )

    def get_doc_hash(self, doc_id: str) -> Optional[str]:
        target = (
            self.shadow_collection
            if getattr(self, "_rebuild_target", None) is not None
            else self.collection
        )
        try:
            res = self._query_in(target, f'doc_id == "{doc_id}"', ["doc_hash"], 1)
            return res[0].get("doc_hash") if res else None
        except Exception:
            # Collection (or field) does not exist yet.
            return None

    @staticmethod
    def _doc_expr(doc_ids: List[str]) -> str:
        escaped = [d.replace("\\", "\\\\").replace('"', '\\"') for d in doc_ids]
        return "doc_id in [" + ", ".join(f'"{d}"' for d in escaped) + "]"

    def delete_by_doc_id(self, doc_id: str) -> int:
        return self.delete_by_doc_ids([doc_id])

    def delete_by_doc_ids(self, doc_ids: List[str]) -> int:
        ids = [doc_id for doc_id in doc_ids if doc_id]
        if not ids:
            return 0
        removed = 0
        # One delete + one flush per batch instead of one per document.
        for start in range(0, len(ids), 100):
            batch = ids[start : start + 100]
            expr = self._doc_expr(batch)
            try:
                removed += self._count(expr)
                self._write_db.delete(expr=expr)
            except Exception:
                logger.warning(
                    "failed to delete %d doc(s) from milvus", len(batch), exc_info=True
                )
        if removed:
            try:
                self.vector_db.client.flush(self._write_collection)
            except Exception:
                logger.warning("flush after delete failed", exc_info=True)
        return removed

    def count_by_doc_id(self, doc_id: str) -> int:
        if not doc_id:
            return 0
        try:
            return self._count(f'doc_id == "{doc_id}"')
        except Exception:
            return 0

    def list_doc_ids(self) -> Set[str]:
        return set(self.doc_summary().keys())

    def doc_summary(self) -> Dict[str, Dict[str, Any]]:
        summary: Dict[str, Dict[str, Any]] = {}
        target = (
            self.shadow_collection
            if getattr(self, "_rebuild_target", None) is not None
            else self.collection
        )
        client = self.vector_db.client
        if not client.has_collection(target):
            return summary
        try:
            iterator = client.query_iterator(
                collection_name=target,
                batch_size=1000,
                filter="",
                output_fields=["doc_id", "doc_hash"],
            )
            while True:
                rows = iterator.next()
                if not rows:
                    iterator.close()
                    break
                for row in rows:
                    doc_id = row.get("doc_id")
                    if not doc_id:
                        continue
                    entry = summary.setdefault(
                        doc_id, {"doc_hash": row.get("doc_hash"), "chunks": 0}
                    )
                    entry["chunks"] += 1
        except Exception:
            logger.warning("failed to build doc summary from milvus", exc_info=True)
        return summary

    def delete_stale_versions(self, doc_id: str, keep_hash: str) -> int:
        if not doc_id or not keep_hash:
            return 0
        escaped_id = doc_id.replace("\\", "\\\\").replace('"', '\\"')
        escaped_hash = keep_hash.replace("\\", "\\\\").replace('"', '\\"')
        expr = (
            f'doc_id == "{escaped_id}" and doc_hash != "{escaped_hash}"'
        )
        return self.delete_by_expr(expr)

    def delete_by_expr(self, expr: str) -> int:
        """Delete by an arbitrary metadata expression; returns chunks removed."""
        try:
            if getattr(self, "_rebuild_target", None) is not None:
                removed = self._count_in(self.shadow_collection, expr)
            else:
                removed = self._count(expr)
        except Exception:
            removed = 0
        try:
            self._write_db.delete(expr=expr)
            if removed:
                self.vector_db.client.flush(self._write_collection)
        except Exception:
            logger.warning("delete by expr failed: %s", expr, exc_info=True)
            return 0
        return removed

    @property
    def shadow_collection(self) -> str:
        return getattr(
            self, "_shadow_name", f"{settings.MILVUS_COLLECTION}__rebuild"
        )

    def _open_collection(self, name: str, create: bool = False):
        """Build a Milvus wrapper for an arbitrary collection name."""
        from langchain_milvus.function import BM25BuiltInFunction

        builtin_function = None
        vector_field = "vector"
        if self.sparse_enabled:
            analyzer = None
            try:
                analyzer = json.loads(settings.HYBRID_ANALYZER)
            except json.JSONDecodeError:
                logger.warning(
                    "invalid HYBRID_ANALYZER=%r, using default", settings.HYBRID_ANALYZER
                )
            builtin_function = BM25BuiltInFunction(
                output_field_names=settings.HYBRID_SPARSE_FIELD,
                analyzer_params=analyzer,
            )
            vector_field = ["vector", settings.HYBRID_SPARSE_FIELD]
        return Milvus(
            embedding_function=self.embeddings,
            connection_args=_connection_args(),
            collection_name=name,
            auto_id=True,
            drop_old=create,
            vector_field=vector_field,
            builtin_function=builtin_function,
        )

    def begin_rebuild(self) -> None:
        """Create an empty shadow collection; the live one keeps serving reads."""
        # Unique physical names guarantee we never drop the collection that
        # the live alias currently points at.
        self._shadow_name = f"{settings.MILVUS_COLLECTION}__r{int(time.time() * 1000)}"
        self._rebuild_target = self._open_collection(self.shadow_collection)
        logger.info("milvus rebuild started in %s", self.shadow_collection)

    @property
    def rebuild_target(self):
        """Handle used for writes while a rebuild is running."""
        return getattr(self, "_rebuild_target", None)

    def commit_rebuild(self) -> None:
        """Point the live collection name at the rebuilt shadow collection.

        Milvus cannot have an alias with the same name as a physical collection,
        so an existing physical `enterprise_kb` is renamed to a legacy name
        first. The read name then resolves through the alias to the shadow.
        """
        client = self.vector_db.client
        live_name = settings.MILVUS_COLLECTION
        shadow = self.shadow_collection
        if not client.has_collection(shadow):
            raise RuntimeError(f"shadow collection {shadow} does not exist")

        aliases = client.list_aliases().get("aliases", [])
        if live_name in aliases:
            previous = client.describe_alias(live_name).get("collection_name")
        elif client.has_collection(live_name):
            # Legacy deployment: a physical collection with the live name.
            previous = live_name
            legacy = f"{live_name}__old_{int(time.time())}"
            client.rename_collection(live_name, legacy)
            self._legacy_collection = legacy
            previous = legacy
            logger.info("renamed physical collection %s -> %s", live_name, legacy)
        else:
            previous = None

        try:
            client.create_alias(shadow, live_name)
        except Exception:
            client.alter_alias(shadow, live_name)

        self._rebuild_target = None
        self._shadow_name = None
        self.collection = live_name
        self.vector_db = self._open_collection(live_name)
        logger.info("milvus rebuild committed: %s -> alias %s", shadow, live_name)

        # Drop the previous physical collection unless it is the legacy one,
        # which drop_legacy_collection() handles.
        if previous and previous != shadow and previous != getattr(
            self, "_legacy_collection", None
        ):
            try:
                client.drop_collection(previous)
                logger.info("dropped previous collection %s", previous)
            except Exception:
                logger.warning("could not drop previous collection %s", previous)

    def drop_legacy_collection(self) -> None:
        legacy = getattr(self, "_legacy_collection", None)
        if not legacy:
            return
        try:
            self.vector_db.client.drop_collection(legacy)
        except Exception:
            logger.warning("could not drop legacy collection %s", legacy, exc_info=True)
        self._legacy_collection = None

    def abort_rebuild(self) -> None:
        client = self.vector_db.client
        shadow = self.shadow_collection
        self._rebuild_target = None
        try:
            if client.has_collection(shadow):
                client.drop_collection(shadow)
        except Exception:
            logger.warning("could not drop shadow collection", exc_info=True)
        self._shadow_name = None
        self.collection = settings.MILVUS_COLLECTION
        self.vector_db = self._open_collection(self.collection)
        logger.info("milvus rebuild aborted, live collection untouched")

    def reset(self) -> None:
        try:
            self.vector_db.client.drop_collection(settings.MILVUS_COLLECTION)
            print(f"Dropped Milvus collection {settings.MILVUS_COLLECTION}.")
        except Exception:
            logger.warning("drop collection failed", exc_info=True)
        self.collection = settings.MILVUS_COLLECTION
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

    def _write_docs(self):
        target = getattr(self, "_rebuild_target", None)
        if target is None:
            # During a rebuild the shadow index starts empty; reading the live
            # hashes here would make every file look unchanged.
            if getattr(self, "_rebuild_path", None):
                return {}
            return self._docs()
        return target.docstore._dict

    def add_documents(self, texts: List[str], metadatas: List[Dict[str, Any]]):
        docs = [Document(page_content=t, metadata=m) for t, m in zip(texts, metadatas, strict=False)]
        if getattr(self, "_rebuild_path", None):
            self._write_add(docs)
            return
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
        target = getattr(self, "_rebuild_target", None) or self.vector_db
        if target is None:
            return 0
        try:
            return int(target.index.ntotal)
        except Exception:
            return 0

    def count_live(self) -> int:
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
        for doc in self._write_docs().values():
            if doc.metadata.get("doc_id") == doc_id:
                return doc.metadata.get("doc_hash")
        return None

    def delete_by_doc_id(self, doc_id: str) -> int:
        return self.delete_by_doc_ids([doc_id])

    def delete_by_doc_ids(self, doc_ids: List[str]) -> int:
        wanted = {doc_id for doc_id in doc_ids if doc_id}
        target = self._write_db
        if not wanted or target is None:
            return 0
        ids = [
            key
            for key, doc in target.docstore._dict.items()
            if doc.metadata.get("doc_id") in wanted
        ]
        if not ids:
            return 0
        target.delete(ids)
        if getattr(self, "_rebuild_path", None):
            # Shadow index is only persisted on commit.
            return len(ids)
        target.save_local(settings.DB_PATH)
        return len(ids)

    def count_by_doc_id(self, doc_id: str) -> int:
        if not doc_id:
            return 0
        return sum(
            1 for doc in self._docs().values() if doc.metadata.get("doc_id") == doc_id
        )

    def list_doc_ids(self) -> Set[str]:
        return set(self.doc_summary().keys())

    def doc_summary(self) -> Dict[str, Dict[str, Any]]:
        summary: Dict[str, Dict[str, Any]] = {}
        for doc in self._write_docs().values():
            doc_id = doc.metadata.get("doc_id")
            if not doc_id:
                continue
            entry = summary.setdefault(
                doc_id, {"doc_hash": doc.metadata.get("doc_hash"), "chunks": 0}
            )
            entry["chunks"] += 1
        return summary

    def delete_stale_versions(self, doc_id: str, keep_hash: str) -> int:
        target = getattr(self, "_rebuild_target", None) or self.vector_db
        if not doc_id or not keep_hash or target is None:
            return 0
        ids = [
            key
            for key, doc in target.docstore._dict.items()
            if doc.metadata.get("doc_id") == doc_id
            and doc.metadata.get("doc_hash") != keep_hash
        ]
        if not ids:
            return 0
        target.delete(ids)
        if getattr(self, "_rebuild_path", None):
            return len(ids)
        target.save_local(settings.DB_PATH)
        return len(ids)

    @property
    def shadow_path(self) -> str:
        return f"{settings.DB_PATH}__rebuild"

    def begin_rebuild(self) -> None:
        shutil.rmtree(self.shadow_path, ignore_errors=True)
        self._rebuild_target = None  # created on first insert
        import tempfile

        self._rebuild_path = tempfile.mkdtemp(prefix="faiss-rebuild-")
        logger.info("faiss rebuild started in %s", self._rebuild_path)

    @property
    def _write_db(self):
        return getattr(self, "_rebuild_target", None) or self.vector_db

    @property
    def _write_collection(self) -> str:
        return "faiss"

    def _write_add(self, docs):
        target = getattr(self, "_rebuild_target", None)
        if target is None:
            target = FAISS.from_documents(docs, self.embeddings)
            self._rebuild_target = target
        else:
            target.add_documents(docs)

    def commit_rebuild(self) -> None:
        target = getattr(self, "_rebuild_target", None)
        if target is None:
            raise RuntimeError("no FAISS rebuild in progress")
        path = getattr(self, "_rebuild_path", None)
        if not path:
            raise RuntimeError("no FAISS rebuild path")
        target.save_local(path)
        # Verify the shadow index loads before touching the live one.
        FAISS.load_local(path, self.embeddings, allow_dangerous_deserialization=True)
        backup = f"{settings.DB_PATH}__old"
        shutil.rmtree(backup, ignore_errors=True)
        if os.path.isdir(settings.DB_PATH):
            os.replace(settings.DB_PATH, backup)
        os.replace(path, settings.DB_PATH)
        shutil.rmtree(backup, ignore_errors=True)
        self._rebuild_target = None
        self._rebuild_path = None
        self.vector_db = FAISS.load_local(
            settings.DB_PATH, self.embeddings, allow_dangerous_deserialization=True
        )
        logger.info("faiss rebuild committed at %s", settings.DB_PATH)

    def abort_rebuild(self) -> None:
        path = getattr(self, "_rebuild_path", None)
        if path:
            shutil.rmtree(path, ignore_errors=True)
        self._rebuild_target = None
        self._rebuild_path = None
        logger.info("faiss rebuild aborted, live index untouched")

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
        # Admin pages call doc_summary() on every refresh; cache it briefly and
        # drop it whenever this process writes.
        self._summary_lock = threading.Lock()
        self._summary_version = -1
        self._summary: Dict[str, Dict[str, Any]] = {}
        self._summary_at = 0.0

        if settings.VECTOR_STORE_TYPE == "milvus":
            self.driver = MilvusDriver(self.embeddings)
        else:
            self.driver = FAISSDriver(self.embeddings)

    def _touch(self) -> None:
        self._version += 1
        self._lexical.invalidate()
        with self._summary_lock:
            self._summary_version = -1
            self._summary = {}
        # Cached answers may cite documents that just changed.
        try:
            from src.cache import answer_cache

            answer_cache.clear()
        except Exception:  # caching must never block writes
            logger.warning("could not clear answer cache", exc_info=True)

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

    def cache_status(self) -> Dict[str, Any]:
        """Hit/miss counters for the process-local caches."""
        embeddings = getattr(self.embeddings, "stats", None)
        return {
            "embeddings": embeddings() if callable(embeddings) else None,
        }

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

    def _observe(self, engine: str) -> None:
        try:
            from src.metrics import observe_retrieval

            observe_retrieval(engine)
        except Exception:  # metrics must never break search
            pass

    def _maybe_rerank(self, query: str, docs: List[Document], k: int) -> List[Document]:
        if not settings.RERANK_ENABLED:
            return docs[:k]
        from src.reranker import rerank_documents

        return rerank_documents(query, docs[: max(k, settings.RERANK_CANDIDATES)], k)

    def search(self, query: str, user_dept: str, k: int = 5) -> List[Document]:
        from src.lexical import rrf_fuse

        if not query.strip():
            return []

        if not settings.HYBRID_ENABLED:
            self._observe("vector")
            candidates = max(
                k, settings.RERANK_CANDIDATES if settings.RERANK_ENABLED else k
            )
            return self._maybe_rerank(
                query, self.driver.search(query, user_dept, candidates), k
            )

        candidates = max(k, settings.HYBRID_CANDIDATES)
        if settings.RERANK_ENABLED:
            candidates = max(candidates, settings.RERANK_CANDIDATES)

        if self.hybrid_mode == "milvus":
            fused = self.driver.hybrid_search(query, user_dept, candidates, candidates)
            if fused is not None:
                self._observe("milvus")
                return self._maybe_rerank(query, fused, k)
            # Server-side hybrid failed: degrade for this query only.
            self._observe("fallback")

        self._observe("local")
        return self._maybe_rerank(
            query, self._local_hybrid(query, user_dept, candidates, rrf_fuse), k
        )

    def _local_hybrid(self, query: str, user_dept: str, candidates: int, fuse):
        vector_docs = self.driver.search(query, user_dept, candidates)
        lexical_docs = self.lexical_search(query, user_dept, candidates)
        return fuse([vector_docs, lexical_docs], candidates)

    def is_empty(self) -> bool:
        return self.driver.is_empty()

    def count(self) -> int:
        return self.driver.count()

    def live_count(self) -> int:
        return self.driver.count_live()

    def get_doc_hash(self, doc_id: str) -> Optional[str]:
        return self.driver.get_doc_hash(doc_id)

    def delete_by_doc_id(self, doc_id: str) -> int:
        removed = self.driver.delete_by_doc_id(doc_id)
        self._touch()
        return removed

    def delete_by_doc_ids(self, doc_ids: List[str]) -> int:
        removed = self.driver.delete_by_doc_ids(doc_ids)
        self._touch()
        return removed

    def delete_stale_versions(self, doc_id: str, keep_hash: str) -> int:
        removed = self.driver.delete_stale_versions(doc_id, keep_hash)
        if removed:
            self._touch()
        return removed

    @property
    def is_rebuilding(self) -> bool:
        return bool(getattr(self.driver, "_rebuild_target", None)) or bool(
            getattr(self.driver, "_rebuild_path", None)
        )

    def begin_rebuild(self) -> None:
        """Start a shadow rebuild; queries keep serving the live index."""
        self.driver.begin_rebuild()

    def commit_rebuild(self) -> None:
        self.driver.commit_rebuild()
        drop = getattr(self.driver, "drop_legacy_collection", None)
        if callable(drop):
            drop()
        self._touch()

    def abort_rebuild(self) -> None:
        self.driver.abort_rebuild()

    def doc_summary(self, max_age: float = 5.0) -> Dict[str, Dict[str, Any]]:
        """doc_id -> {doc_hash, chunks}, cached for a few seconds.

        The cache is keyed by the write version, so ingestion/deletion in this
        process always invalidates it. Writes from another process are picked up
        after `max_age` seconds.
        """
        now = time.monotonic()
        with self._summary_lock:
            fresh = (
                self._summary_version == self._version
                and now - self._summary_at < max_age
            )
            if fresh:
                return self._summary.copy()
        summary = self.driver.doc_summary()
        with self._summary_lock:
            self._summary_version = self._version
            self._summary = summary
            self._summary_at = time.monotonic()
        return summary.copy()

    def count_by_doc_id(self, doc_id: str) -> int:
        return self.driver.count_by_doc_id(doc_id)

    def list_doc_ids(self) -> Set[str]:
        return self.driver.list_doc_ids()

    def reset(self) -> None:
        self.driver.reset()
        self._touch()


# Singleton instance for global use
kb_engine = KnowledgeBaseEngine()
