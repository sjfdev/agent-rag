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
    LLM_MAX_TOKENS: int = 512
    LLM_MAX_RETRIES: int = 2
    AGENT_RECURSION_LIMIT: int = 6

    # Vector Store Config
    VECTOR_STORE_TYPE: Literal["faiss", "milvus"] = "faiss"
    DB_PATH: str = "./db/vector_db"
    RETRIEVAL_K: int = 4
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
