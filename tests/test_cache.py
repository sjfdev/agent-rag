"""Caches: FAQ answers (LRU/TTL, scoping) and query embeddings."""

import time

from src.cache import AnswerCache, normalize_question
from src.embedding_cache import CachedEmbeddings


def test_normalize_question():
    assert normalize_question("  What is the VPN-Policy? ") == "what is the vpn policy"
    assert normalize_question("访客 Wi-Fi 密码？") == "访客 wi fi 密码"
    assert normalize_question("") == ""


def test_answer_cache_keying_and_scope():
    cache = AnswerCache(max_entries=4, ttl_seconds=60)
    cache.put("Tech", "What is the code review process?", {"response": "a"})
    assert cache.get("Tech", "what is the code review PROCESS")["response"] == "a"
    assert cache.get("Finance", "What is the code review process?") is None
    assert cache.get("Tech", "unrelated") is None


def test_answer_cache_lru_eviction():
    cache = AnswerCache(max_entries=2, ttl_seconds=60)
    for question in ("q1", "q2", "q3"):
        cache.put("Tech", question, {"response": question})
    assert cache.get("Tech", "q1") is None
    assert cache.get("Tech", "q3")["response"] == "q3"


def test_answer_cache_ttl_expiry():
    cache = AnswerCache(max_entries=4, ttl_seconds=0.05)
    cache.put("Tech", "q", {"response": "a"})
    time.sleep(0.1)
    assert cache.get("Tech", "q") is None


def test_answer_cache_clear_and_stats():
    cache = AnswerCache(max_entries=4, ttl_seconds=60)
    cache.put("Tech", "q", {"response": "a"})
    cache.get("Tech", "q")
    cache.get("Tech", "miss")
    stats = cache.stats()
    assert stats["entries"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    cache.clear()
    assert cache.stats()["entries"] == 0


class _FakeEmbeddings:
    def __init__(self):
        self.query_calls = 0
        self.document_calls = 0

    def embed_query(self, text):
        self.query_calls += 1
        return [float(len(text)), 1.0]

    def embed_documents(self, texts):
        self.document_calls += 1
        return [[float(len(t)), 2.0] for t in texts]


def test_embedding_cache_hits_and_bypasses_documents():
    inner = _FakeEmbeddings()
    cached = CachedEmbeddings(inner, namespace="test", max_entries=2)
    assert cached.embed_query("hello") == cached.embed_query("hello")
    assert inner.query_calls == 1
    cached.embed_documents(["a", "b"])
    assert inner.document_calls == 1
    assert cached.stats()["namespace"] == "test"


def test_embedding_cache_is_bounded():
    inner = _FakeEmbeddings()
    cached = CachedEmbeddings(inner, namespace="test", max_entries=2)
    for text in ("a", "b", "c"):
        cached.embed_query(text)
    assert cached.stats()["entries"] == 2


def test_embedding_cache_namespace_changes_with_model(monkeypatch):
    from src import embedding_cache
    from src.config import settings

    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "local")
    monkeypatch.setattr(settings, "LOCAL_EMBEDDING_MODEL", "model-a")
    first = embedding_cache.cache_namespace()
    monkeypatch.setattr(settings, "LOCAL_EMBEDDING_MODEL", "model-b")
    assert embedding_cache.cache_namespace() != first


def test_embedding_cache_proxies_unknown_attributes():
    inner = _FakeEmbeddings()
    inner.model_name = "demo"
    cached = CachedEmbeddings(inner, namespace="test")
    assert cached.model_name == "demo"
