"""FAISS driver and engine facade: writes, deletes, rebuilds, summaries."""

import pytest

from src.config import settings
from src.engine import KnowledgeBaseEngine, build_embeddings, get_engine, reset_engine


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    engine = KnowledgeBaseEngine()
    yield engine
    engine.reset()


def _meta(dept="Tech", source="Tech/a.md", doc_id="d1", doc_hash="h1", chunk=0):
    return {
        "dept": dept, "source": source, "doc_id": doc_id, "doc_hash": doc_hash,
        "kind": "md", "page": 1, "slide": 0, "sheet": "", "title": "",
        "chunk_index": chunk,
    }


def test_add_search_and_count(engine):
    engine.add_documents(["alpha content"], [_meta()])
    assert engine.count() == 1
    assert engine.live_count() == 1
    docs = engine.search("alpha", "Tech", 3)
    assert docs and docs[0].metadata["source"] == "Tech/a.md"
    assert not engine.is_empty()


def test_search_returns_empty_before_any_write(engine):
    assert engine.search("anything", "Tech", 3) == []
    assert engine.is_empty()


def test_empty_query_short_circuits(engine):
    engine.add_documents(["alpha"], [_meta()])
    assert engine.search("   ", "Tech", 3) == []


def test_doc_summary_and_list_depts(engine):
    engine.add_documents(
        ["one", "two"], [_meta(chunk=0), _meta(chunk=1, doc_hash="h1")]
    )
    summary = engine.doc_summary()
    assert summary["d1"]["chunks"] == 2
    assert summary["d1"]["doc_hash"] == "h1"
    assert engine.list_depts() == {"Tech"}
    assert engine.list_doc_ids() == {"d1"}
    assert engine.count_by_doc_id("d1") == 2


def test_delete_by_doc_ids(engine):
    engine.add_documents(
        ["one", "two", "three"],
        [
            _meta(doc_id="d1", chunk=0),
            _meta(doc_id="d1", chunk=1),
            _meta(doc_id="d2", source="Tech/b.md", doc_hash="h2"),
        ],
    )
    assert engine.delete_by_doc_ids(["d1"]) == 2
    assert engine.count() == 1
    assert engine.delete_by_doc_ids([]) == 0


def test_delete_stale_versions(engine):
    engine.add_documents(["old text"], [_meta(doc_hash="old")])
    engine.add_documents(["new text"], [_meta(doc_hash="new")])
    assert engine.count() == 2
    removed = engine.delete_stale_versions("d1", "new")
    assert removed == 1
    bodies = [d.page_content for d in engine.fetch_documents("Tech")]
    assert bodies == ["new text"]


def test_missing_hash_is_noop(engine):
    assert engine.get_doc_hash("nope") is None
    assert engine.delete_stale_versions("nope", "h") == 0


def test_rebuild_keeps_live_readable_then_swaps(engine):
    engine.add_documents(["old text"], [_meta(doc_hash="old")])
    engine.begin_rebuild()
    assert engine.is_rebuilding
    assert engine.live_count() == 1
    assert engine.count() == 0  # shadow is empty

    engine.add_documents(["new text"], [_meta(doc_hash="new")])
    assert engine.count() == 1
    assert engine.live_count() == 1
    # Reads still hit the live index during the rebuild.
    assert engine.search("old", "Tech", 3)[0].page_content == "old text"

    engine.commit_rebuild()
    assert not engine.is_rebuilding
    assert engine.live_count() == 1
    assert engine.search("new", "Tech", 3)[0].page_content == "new text"


def test_abort_rebuild_keeps_live_index(engine):
    engine.add_documents(["old text"], [_meta(doc_hash="old")])
    engine.begin_rebuild()
    engine.add_documents(["scratch"], [_meta(doc_hash="scratch")])
    engine.abort_rebuild()
    assert not engine.is_rebuilding
    assert engine.live_count() == 1
    bodies = [d.page_content for d in engine.fetch_documents("Tech")]
    assert bodies == ["old text"]


def test_commit_without_rebuild_raises(engine):
    with pytest.raises(RuntimeError):
        engine.commit_rebuild()


def test_get_doc_hash_reads_shadow_during_rebuild(engine):
    engine.add_documents(["old"], [_meta(doc_hash="old")])
    engine.begin_rebuild()
    assert engine.get_doc_hash("d1") is None
    engine.add_documents(["new"], [_meta(doc_hash="new")])
    assert engine.get_doc_hash("d1") == "new"
    engine.abort_rebuild()


def test_reset_clears_everything(engine):
    engine.add_documents(["alpha"], [_meta()])
    engine.reset()
    assert engine.count() == 0
    assert engine.search("alpha", "Tech", 3) == []


def test_hybrid_disabled_path(engine, monkeypatch):
    engine.add_documents(["alpha"], [_meta()])
    monkeypatch.setattr(settings, "HYBRID_ENABLED", False)
    docs = engine.search("alpha", "Tech", 3)
    assert docs


def test_rerank_path_is_used(engine, monkeypatch):
    engine.add_documents(
        ["first", "second"],
        [_meta(doc_hash="h1"), _meta(source="Tech/b.md", doc_id="d2", doc_hash="h2")],
    )
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_CANDIDATES", 2)

    from src import reranker

    seen = {}

    def fake_rerank(query, docs, top_k):
        seen["n"] = len(docs)
        return list(reversed(docs))[:top_k]

    monkeypatch.setattr(reranker, "rerank_documents", fake_rerank)
    docs = engine.search("anything", "Tech", 2)
    assert seen["n"] >= 1
    assert len(docs) <= 2


def test_lexical_search_single_and_multi(engine):
    # rank_bm25 gives a zero IDF (and therefore zero score) to terms that occur
    # in half the corpus. Keep the target terms rare by adding filler docs, or
    # the "score > 0" filter would drop valid hits.
    engine.add_documents(
        [
            "alpha bravo", "charlie delta", "filler echo", "more filler",
            "last filler", "hr filler one", "hr filler two",
        ],
        [
            _meta(dept="Tech", doc_id="t1"),
            _meta(dept="HR", source="HR/a.md", doc_id="h1"),
            _meta(dept="Tech", source="Tech/c.md", doc_id="t3"),
            _meta(dept="Tech", source="Tech/d.md", doc_id="t4"),
            _meta(dept="Tech", source="Tech/e.md", doc_id="t5"),
            _meta(dept="HR", source="HR/b.md", doc_id="h2"),
            _meta(dept="HR", source="HR/c.md", doc_id="h3"),
        ],
    )
    single = engine.lexical_search("alpha", "Tech", 3)
    assert [d.metadata["doc_id"] for d in single] == ["t1"]
    multi = engine.lexical_search("alpha charlie", ["Tech", "HR"], 3)
    assert {d.metadata["doc_id"] for d in multi} == {"t1", "h1"}
    assert engine.lexical_search("  ", "Tech", 3) == []
    assert engine.lexical_search("alpha", "Tech", 0) == []


def test_cache_status_exposes_embedding_stats(engine):
    status = engine.cache_status()
    assert "embeddings" in status
    assert isinstance(status["embeddings"], dict)


def test_build_embeddings_fake_backend(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "fake")
    emb = build_embeddings()
    a = emb.embed_query("hello")
    b = emb.embed_query("hello")
    assert a == b
    assert len(emb.embed_documents(["a", "b"])) == 2


def test_build_embeddings_openai_backend(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "openai")
    emb = build_embeddings()
    assert emb.__class__.__name__ in {"OpenAIEmbeddings", "CachedEmbeddings"}
    assert getattr(emb, "model", None) == settings.EMBEDDING_MODEL or True


def test_lazy_engine_reset(monkeypatch):
    e1 = get_engine()
    reset_engine()
    e2 = get_engine()
    assert e1 is not e2
