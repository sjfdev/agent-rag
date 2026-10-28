import os
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from langchain_openai import OpenAIEmbeddings
from langchain_milvus import Milvus
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from src.config import settings


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

class MilvusDriver(BaseVectorStore):
    """Enterprise-scale driver using Milvus distributed cluster."""
    def __init__(self, embeddings):
        self.vector_db = Milvus(
            embedding_function=embeddings,
            connection_args={"host": "localhost", "port": "19530"},
            collection_name="enterprise_kb",
            auto_id=True,
            drop_old=False
        )

    def add_documents(self, texts: List[str], metadatas: List[Dict[str, Any]]):
        docs = [Document(page_content=t, metadata=m) for t, m in zip(texts, metadatas)]
        self.vector_db.add_documents(docs)

    def search(self, query: str, user_dept: str, k: int = 5) -> List[Document]:
        expr = f"dept == '{user_dept}'"
        return self.vector_db.similarity_search(query, k=k, expr=expr)

    def is_empty(self) -> bool:
        try:
            res = self.vector_db.client.query(
                collection_name="enterprise_kb",
                filter="",
                output_fields=["count(*)"],
            )
            return int(res[0]["count(*)"]) == 0
        except Exception:
            return True

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
                allow_dangerous_deserialization=True
            )
        except Exception:
            self.vector_db = None

    def add_documents(self, texts: List[str], metadatas: List[Dict[str, Any]]):
        docs = [Document(page_content=t, metadata=m) for t, m in zip(texts, metadatas)]
        if self.vector_db is None:
            self.vector_db = FAISS.from_documents(docs, self.embeddings)
        else:
            self.vector_db.add_documents(docs)
        self.vector_db.save_local(settings.DB_PATH)

    def search(self, query: str, user_dept: str, k: int = 5) -> List[Document]:
        if self.vector_db is None:
            return []

        # FAISS doesn't support server-side filtering like Milvus.
        # We implement post-retrieval filtering to maintain permission control.
        # Fetch a wider candidate pool, then filter down to the user's department.
        pool = max(k * 20, 50)
        results = self.vector_db.similarity_search(query, k=pool)
        filtered = [doc for doc in results if doc.metadata.get("dept") == user_dept]
        return filtered[:k]

    def is_empty(self) -> bool:
        return self.vector_db is None or self.vector_db.index.ntotal == 0

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
        return self.driver.search(query, user_dept, k)

    def is_empty(self) -> bool:
        return self.driver.is_empty()

# Singleton instance for global use
kb_engine = KnowledgeBaseEngine()
