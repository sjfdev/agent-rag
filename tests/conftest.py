"""Shared pytest fixtures.

Tests run fully offline: fake embeddings, a temp FAISS directory and no LLM.
Environment variables are set before importing the app so the engine singleton
is built with the fake backend.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _offline_env(tmp_path_factory):
    """Configure the process for offline, deterministic tests."""
    root = tmp_path_factory.mktemp("agent-rag")
    os.environ.setdefault("EMBEDDING_BACKEND", "fake")
    os.environ.setdefault("VECTOR_STORE_TYPE", "faiss")
    os.environ.setdefault("DB_PATH", str(root / "vector_db"))
    os.environ.setdefault("DATA_DIR", str(root / "data"))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("FAQ_CACHE_ENABLED", "true")
    # Imported here so settings pick up the environment above.
    import src  # noqa: F401
    from src.config import settings

    settings.EMBEDDING_BACKEND = "fake"
    settings.VECTOR_STORE_TYPE = "faiss"
    settings.DB_PATH = str(root / "vector_db")
    settings.DATA_DIR = str(root / "data")
    yield


