"""Hybrid retrieval primitives: tokenizer, BM25, RRF, capability decisions."""

import pytest

from src.config import settings
from src.engine import (
    FakeEmbeddings,
    MilvusCapabilities,
    MilvusDriver,
    hits_to_documents,
)
from src.hybrid import parse_version, resolve_hybrid_mode, version_at_least
from src.lexical import LexicalCache, LexicalIndex, rrf_fuse, tokenize


def doc(text, doc_id, chunk, source="a.pdf"):
    from langchain_core.documents import Document

    return Document(
        page_content=text,
        metadata={"doc_id": doc_id, "chunk_index": chunk, "source": source},
    )


def test_latin_tokens():
    assert tokenize("VPN Access 2026") == ["vpn", "access", "2026"]


def test_cjk_unigrams_and_bigrams():
    tokens = tokenize("访客密码")
    assert "访" in tokens and "密" in tokens
    assert "访客" in tokens and "密码" in tokens


def test_bm25_finds_exact_identifier():
    docs = [
        doc("Passwords rotate every 90 days.", "d1", 0),
        doc("Visitor Wi-Fi uses DEMO-GUEST at reception.", "d2", 0),
        doc("Annual leave is 20 days.", "d3", 0),
    ]
    index = LexicalIndex(docs)
    hits = index.search("DEMO-GUEST", 2)
    assert hits and hits[0].metadata["doc_id"] == "d2"


def test_bm25_empty_query_and_absent_term():
    docs = [doc("alpha", "d1", 0)]
    index = LexicalIndex(docs)
    assert index.search("", 2) == []
    assert index.search("kubernetes", 2) == []


def test_rrf_fuses_and_deduplicates():
    docs = [doc("a", "d1", 0), doc("b", "d2", 0), doc("c", "d3", 0)]
    fused = rrf_fuse([[docs[2], docs[0]], [docs[0], docs[1]]], 3)
    keys = [(d.metadata["doc_id"], d.metadata["chunk_index"]) for d in fused]
    assert len(keys) == len(set(keys))
    assert keys[0] == ("d1", 0)
    assert len(rrf_fuse([[docs[0]], [docs[1]]], 1)) == 1


def test_lexical_cache_versioning():
    docs = [doc("alpha", "d1", 0)]
    loads = {"n": 0}

    def loader(_dept):
        loads["n"] += 1
        return docs

    cache = LexicalCache()
    cache.get("Tech", 1, loader)
    cache.get("Tech", 1, loader)
    assert loads["n"] == 1
    cache.get("Tech", 2, loader)
    assert loads["n"] == 2
    cache.invalidate()
    cache.get("Tech", 2, loader)
    assert loads["n"] == 3


def test_stale_build_is_not_cached():
    docs = [doc("alpha", "d1", 0)]
    loads = {"n": 0}

    def racing_loader(_dept):
        loads["n"] += 1
        cache._version = 99
        return docs

    def loader(_dept):
        loads["n"] += 1
        return docs

    cache = LexicalCache()
    cache.get("Tech", 3, racing_loader)
    cache.get("Tech", 3, loader)
    assert loads["n"] == 2


@pytest.mark.parametrize(
    "raw, expected",
    [("v2.5.4", (2, 5, 4)), ("2.5", (2, 5, 0)), ("unknown", None), (None, None)],
)
def test_parse_version(raw, expected):
    assert parse_version(raw) == expected


def test_version_comparison():
    assert version_at_least("v2.5.4", "2.5.0")
    assert not version_at_least("v2.4.0", "2.5.0")


@pytest.mark.parametrize(
    "configured, enabled, supported, expected",
    [
        ("auto", True, True, "milvus"),
        ("auto", True, False, "local"),
        ("local", True, True, "local"),
        ("milvus", True, False, "milvus"),
        ("auto", False, True, "off"),
    ],
)
def test_resolve_hybrid_mode(configured, enabled, supported, expected):
    assert resolve_hybrid_mode(configured, enabled, supported) == expected


def test_resolve_rejects_unknown_engine():
    with pytest.raises(ValueError):
        resolve_hybrid_mode("bogus", True, True)


def _decide(caps, engine):
    driver = MilvusDriver.__new__(MilvusDriver)
    driver.capabilities = caps
    driver.collection = "test"
    old = settings.HYBRID_ENGINE
    try:
        settings.HYBRID_ENGINE = engine
        return driver._decide_sparse()
    finally:
        settings.HYBRID_ENGINE = old


@pytest.fixture()
def hybrid_enabled():
    old = settings.HYBRID_ENABLED
    settings.HYBRID_ENABLED = True
    yield
    settings.HYBRID_ENABLED = old


def test_capabilities(hybrid_enabled):
    ready = MilvusCapabilities(
        server_version="v2.5.4", version_ok=True,
        collection_exists=True, sparse_field=True, bm25_index=True,
    )
    old_schema = MilvusCapabilities(
        server_version="v2.5.4", version_ok=True,
        collection_exists=True, sparse_field=False, bm25_index=False,
    )
    v24 = MilvusCapabilities(server_version="v2.4.0", version_ok=False)
    fresh = MilvusCapabilities(server_version="v2.5.4", version_ok=True)

    assert ready.sparse_ready
    assert not old_schema.sparse_ready
    assert fresh.sparse_ready
    assert _decide(ready, "auto") is True
    assert _decide(v24, "auto") is False
    assert _decide(old_schema, "auto") is False
    assert _decide(ready, "local") is False
    assert _decide(ready, "milvus") is True
    with pytest.raises(RuntimeError):
        _decide(v24, "milvus")
    with pytest.raises(RuntimeError):
        _decide(old_schema, "milvus")


def test_disabled_engine(hybrid_enabled):
    ready = MilvusCapabilities(server_version="v2.5.4", version_ok=True)
    old = settings.HYBRID_ENABLED
    settings.HYBRID_ENABLED = False
    try:
        assert _decide(ready, "auto") is False
    finally:
        settings.HYBRID_ENABLED = old


def test_hit_conversion():
    hits = [
        {"id": 7, "distance": 0.1,
         "entity": {"text": "alpha", "source": "a.pdf", "dept": "Tech"}},
        {"text": "beta", "source": "b.pdf", "pk": 9},
    ]
    docs = hits_to_documents(hits)
    assert len(docs) == 2
    assert docs[0].page_content == "alpha"
    assert docs[0].metadata["source"] == "a.pdf"
    assert "id" not in docs[0].metadata and "pk" not in docs[1].metadata
    assert hits_to_documents([]) == []


def test_milvus_delete_expression_escaping():
    expr = MilvusDriver._doc_expr(["a", 'we"ird', "back\\slash"])
    assert expr.startswith("doc_id in [") and expr.endswith("]")
    assert '\\"' in expr
    assert "\\\\" in expr


class _RecordingEmbeddings:
    def __init__(self):
        self.calls = 0

    def embed_query(self, text):
        self.calls += 1
        return [float(len(text)), 1.0]

    def embed_documents(self, texts):
        self.calls += 1
        return [[float(len(t)), 2.0] for t in texts]


def test_fake_embeddings_are_deterministic_and_normalised():
    emb = FakeEmbeddings(dim=32)
    a = emb.embed_query("hello")
    b = emb.embed_query("hello")
    c = emb.embed_query("other")
    assert a == b
    assert a != c
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9
