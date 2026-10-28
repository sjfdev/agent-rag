import os
from pydantic_settings import BaseSettings, SettingsConfigDict

# Speed up model downloads in mainland China (must be set before transformers import)
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

class Settings(BaseSettings):
    # Loads .env if present; real env vars take precedence.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM Config (any OpenAI-compatible endpoint: Ollama / vLLM / DeepSeek / OpenAI)
    # Ollama default: http://localhost:11434/v1
    # vLLM default:   http://localhost:8000/v1
    LLM_API_BASE: str = "http://localhost:11434/v1"
    LLM_API_KEY: str = "ollama"
    LLM_MODEL: str = "qwen2.5:latest"

    # Vector Store Config
    VECTOR_STORE_TYPE: str = "faiss"  # "milvus" or "faiss"
    DB_PATH: str = "./db/vector_db"

    # Embedding Config
    # "local"  -> sentence-transformers (offline, no API key needed)
    # "openai" -> OpenAI-compatible /embeddings endpoint
    EMBEDDING_BACKEND: str = "local"
    LOCAL_EMBEDDING_MODEL: str = "BAAI/bge-small-zh-v1.5"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

settings = Settings()
