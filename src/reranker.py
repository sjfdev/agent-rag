from sentence_transformers import CrossEncoder
from src.engine import kb_engine
from typing import List

from langchain_core.documents import Document

class EnterpriseReranker:
    def __init__(self, model_name: str = "BAAI/bge-reranker-base"):
        # Load a Cross-Encoder model for high-precision reranking
        # This model compares (Query, Document) pairs and outputs a relevance score
        self.model = CrossEncoder(model_name)

    def rerank(self, query: str, docs: List[Document], top_k: int = 3) -> List[Document]:
        """
        Rerank documents using a Cross-Encoder to improve retrieval precision.
        """
        if not docs:
            return []

        # Prepare pairs for the model: [[query, doc1], [query, doc2], ...]
        pairs = [[query, doc.page_content] for doc in docs]
        scores = self.model.predict(pairs)

        # Sort documents by score in descending order
        scored_docs = sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)
        
        # Return only the top_k most relevant documents
        return [doc for score, doc in scored_docs[:top_k]]

# Update the search method in the system by integrating this into a coordinator
class RAGCoordinator:
    def __init__(self):
        self.engine = kb_engine
        self.reranker = EnterpriseReranker()

    def retrieve_best_context(self, query: str, user_dept: str):
        # 1. First-stage retrieval: Get top 20 candidates from Milvus (Fast)
        candidates = self.engine.search(query, user_dept, k=20)
        
        # 2. Second-stage reranking: Get top 3 absolute best (Precise)
        best_docs = self.reranker.rerank(query, candidates, top_k=3)
        
        return best_docs
