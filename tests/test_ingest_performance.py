"""Ingestion throughput knobs: embedding batch size and phase timing.

入库吞吐配置：embedding batch size 与阶段耗时。
"""

import time

from src import embedding_cache
from src.config import settings


def test_local_embedding_batch_size_is_configurable(monkeypatch):
    import langchain_huggingface

    captured = {}

    class DummyEmbeddings:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_huggingface, "HuggingFaceEmbeddings", DummyEmbeddings)
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "local")
    monkeypatch.setattr(settings, "EMBEDDING_BATCH_SIZE", 64)

    from src.engine import _raw_embeddings

    _raw_embeddings()
    assert captured["encode_kwargs"]["batch_size"] == 64
    assert captured["encode_kwargs"]["normalize_embeddings"] is True


def test_embedding_batch_size_is_clamped(monkeypatch):
    import langchain_huggingface

    captured = {}

    class DummyEmbeddings:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_huggingface, "HuggingFaceEmbeddings", DummyEmbeddings)
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "local")
    monkeypatch.setattr(settings, "EMBEDDING_BATCH_SIZE", 10000)

    from src.engine import _raw_embeddings

    _raw_embeddings()
    assert captured["encode_kwargs"]["batch_size"] == 512


def test_document_embedding_timer_separates_embed_from_write():
    class SlowEmbeddings:
        def embed_documents(self, texts):
            time.sleep(0.01)
            return [[0.0] for _ in texts]

        def embed_query(self, text):
            return [0.0]

    cached = embedding_cache.CachedEmbeddings(
        SlowEmbeddings(), namespace="timing-test", max_entries=4
    )
    embedding_cache.reset_document_timer()
    cached.embed_documents(["a", "b"])
    elapsed = embedding_cache.document_embedding_ms()
    assert elapsed >= 5  # milliseconds, not seconds
