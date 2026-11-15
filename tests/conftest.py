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

# Applied at import time (before any test module imports the app): a developer's
# real .env may enable jwt/local login and point at the live accounts file.
# Tests start in the legacy "none" mode and opt in explicitly.
os.environ["AUTH_MODE"] = "none"
os.environ["AUTH_LOCAL_LOGIN_ENABLED"] = "false"
os.environ["AUTH_JWT_SECRET"] = ""
os.environ["AUTH_JWKS_URL"] = ""
os.environ["AUTH_JWKS_FILE"] = ""


@pytest.fixture(scope="session", autouse=True)
def _offline_env(tmp_path_factory):
    """Configure the process for offline, deterministic tests."""
    root = tmp_path_factory.mktemp("agent-rag")
    os.environ.setdefault("EMBEDDING_BACKEND", "fake")
    os.environ.setdefault("VECTOR_STORE_TYPE", "faiss")
    os.environ.setdefault("DB_PATH", str(root / "vector_db"))
    os.environ.setdefault("DATA_DIR", str(root / "data"))
    os.environ.setdefault("BACKUP_DIR", str(root / "backups"))
    os.environ.setdefault("SESSIONS_DB_PATH", str(root / "sessions.db"))
    os.environ.setdefault("SESSION_SUMMARY_ENABLED", "false")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("FAQ_CACHE_ENABLED", "true")
    # A developer's real .env may point at the live accounts file.
    os.environ["AUTH_USERS_FILE"] = str(root / "users.json")
    # Never read or write the developer's real console overrides.
    # 绝不读写开发者本机真实的控制台覆盖文件。
    os.environ.setdefault("MODEL_SETTINGS_FILE", str(root / "model_settings.json"))
    # Imported here so settings pick up the environment above.
    import src  # noqa: F401
    from src.config import settings

    settings.EMBEDDING_BACKEND = "fake"
    settings.VECTOR_STORE_TYPE = "faiss"
    settings.DB_PATH = str(root / "vector_db")
    settings.DATA_DIR = str(root / "data")
    settings.BACKUP_DIR = str(root / "backups")
    settings.SESSIONS_DB_PATH = str(root / "sessions.db")
    settings.SESSION_SUMMARY_ENABLED = False
    settings.AUTH_MODE = "none"
    settings.AUTH_LOCAL_LOGIN_ENABLED = False
    settings.AUTH_JWT_SECRET = ""
    settings.AUTH_JWKS_URL = ""
    settings.AUTH_JWKS_FILE = ""
    settings.AUTH_USERS_FILE = str(root / "users.json")
    settings.MODEL_SETTINGS_FILE = os.environ["MODEL_SETTINGS_FILE"]
    from src import model_settings

    model_settings.reset_state()
    yield


