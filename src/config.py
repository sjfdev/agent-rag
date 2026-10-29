from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Loads .env if present; real env vars take precedence.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM Config (any OpenAI-compatible endpoint: Ollama / vLLM / DeepSeek / OpenAI)
    # Ollama default: http://localhost:11434/v1
    # vLLM default:   http://localhost:8000/v1
    LLM_API_BASE: str = "http://localhost:11434/v1"
    LLM_API_KEY: str = "ollama"
    LLM_MODEL: str = "qwen2.5:latest"
    LLM_TIMEOUT: int = 30
    LLM_MAX_TOKENS: int = 256
    LLM_MAX_RETRIES: int = 2
    AGENT_RECURSION_LIMIT: int = 6

    # Ingestion / admin
    DATA_DIR: str = "./data"
    ADMIN_TOKEN: str = ""      # empty => admin API only reachable from loopback
    MAX_UPLOAD_MB: int = 50

    # Vector Store Config
    VECTOR_STORE_TYPE: Literal["faiss", "milvus"] = "faiss"
    DB_PATH: str = "./db/vector_db"
    RETRIEVAL_K: int = 4
    # Optional cross-encoder reranking of the first-stage candidates. Disabled
    # by default: on CPU it adds noticeable latency and needs the model cached.
    RERANK_ENABLED: bool = False
    RERANK_MODEL: str = "BAAI/bge-reranker-base"
    RERANK_CANDIDATES: int = 12
    # /health reports "down" when fewer chunks than this are indexed, which
    # makes a load balancer stop routing to an empty instance.
    HEALTH_MIN_CHUNKS: int = 1
    # Hybrid retrieval: fuse vector and BM25 results with reciprocal rank
    # fusion before optional reranking.
    HYBRID_ENABLED: bool = True
    HYBRID_CANDIDATES: int = 20
    # Cap the in-memory BM25 corpus per department (0 = unlimited).
    HYBRID_MAX_DOCS: int = 20000
    # auto  -> server-side sparse BM25 when Milvus >= 2.5 and the collection
    #          has a sparse field, otherwise the in-process BM25 index
    # local -> always in-process BM25 (works with FAISS and Milvus)
    # milvus-> require server-side sparse BM25 (fails loudly if unavailable)
    HYBRID_ENGINE: Literal["auto", "local", "milvus"] = "auto"
    # Field populated by the Milvus BM25 function. Existing collections need
    # `python run_ingestion.py --reset` after upgrading to 2.5.
    HYBRID_SPARSE_FIELD: str = "sparse"
    # Analyzer used by the server-side BM25 function (JSON). "chinese" handles
    # mixed Chinese/Latin text best for this corpus.
    HYBRID_ANALYZER: str = '{"type": "chinese"}'
    # Optional bearer token for GET /metrics (empty = open, rely on the network).
    METRICS_TOKEN: str = ""
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: str = "19530"
    MILVUS_COLLECTION: str = "enterprise_kb"
    MILVUS_URI: str = ""

    # Embedding Config
    # "local"  -> sentence-transformers (offline, no API key needed)
    # "openai" -> OpenAI-compatible /embeddings endpoint
    EMBEDDING_BACKEND: Literal["local", "openai"] = "local"
    LOCAL_EMBEDDING_MODEL: str = "BAAI/bge-small-zh-v1.5"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

settings = Settings()
