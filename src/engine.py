import logging
import os
import shutil
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Set

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_milvus import Milvus
from langchain_openai import OpenAIEmbeddings

from src.config import settings
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


class MilvusDriver(BaseVectorStore):
    """Enterprise-scale driver using Milvus distributed cluster."""

    def __init__(self, embeddings):
        self.embeddings = embeddings
        self.collection = settings.MILVUS_COLLECTION
        connection_args = {"host": settings.MILVUS_HOST, "port": settings.MILVUS_PORT}
        if settings.MILVUS_URI:
            connection_args = {"uri": settings.MILVUS_URI}
        self.vector_db = Milvus(
            embedding_function=embeddings,
            connection_args=connection_args,
            collection_name=self.collection,
            auto_id=True,
            drop_old=False,
        )

    def _query(
        self, expr: str, fields: List[str], limit: int = 1
    ) -> List[Dict[str, Any]]:
        return self.vector_db.client.query(
            collection_name=self.collection,
            filter=expr,
            output_fields=fields,
            limit=limit,
        )

    def _count(self, expr: str) -> int:
        # Milvus rejects count(*) when a pagination limit is present.
        res = self.vector_db.client.query(
            collection_name=self.collection,
            filter=expr,
            output_fields=["count(*)"],
        )
        return int(res[0]["count(*)"]) if res else 0

    def add_documents(self, texts: List[str], metadatas: List[Dict[str, Any]]):
        docs = [Document(page_content=t, metadata=m) for t, m in zip(texts, metadatas)]
        self.vector_db.add_documents(docs)

    def search(self, query: str, user_dept: str, k: int = 5) -> List[Document]:
        safe_dept = validate_dept(user_dept)
        expr = f"dept == '{safe_dept}'"
        return self.vector_db.similarity_search(query, k=k, expr=expr)

    def is_empty(self) -> bool:
        try:
            return self._count("") == 0
        except Exception:
            return True

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
        return self.vector_db is None or self.vector_db.index.ntotal == 0

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
        self.embeddings = build_embeddings()

        if settings.VECTOR_STORE_TYPE == "milvus":
            self.driver = MilvusDriver(self.embeddings)
        else:
            self.driver = FAISSDriver(self.embeddings)

    def add_documents(self, texts: List[str], metadatas: List[Dict[str, Any]]):
        self.driver.add_documents(texts, metadatas)

    def search(self, query: str, user_dept: str, k: int = 5) -> List[Document]:
        if settings.RERANK_ENABLED:
            from src.reranker import rerank_documents

            candidates = self.driver.search(
                query, user_dept, max(k, settings.RERANK_CANDIDATES)
            )
            return rerank_documents(query, candidates, k)
        return self.driver.search(query, user_dept, k)

    def is_empty(self) -> bool:
        return self.driver.is_empty()

    def get_doc_hash(self, doc_id: str) -> Optional[str]:
        return self.driver.get_doc_hash(doc_id)

    def delete_by_doc_id(self, doc_id: str) -> int:
        return self.driver.delete_by_doc_id(doc_id)

    def count_by_doc_id(self, doc_id: str) -> int:
        return self.driver.count_by_doc_id(doc_id)

    def list_doc_ids(self) -> Set[str]:
        return self.driver.list_doc_ids()

    def reset(self) -> None:
        self.driver.reset()


# Singleton instance for global use
kb_engine = KnowledgeBaseEngine()
