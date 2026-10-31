"""Citation merging, scores and rerank timing."""

import pytest
from langchain_core.messages import HumanMessage

from src import agent as agent_module
from src.engine import _l2_to_score, hits_to_documents


def _doc(text="snippet", source="Tech/a.md", page=1, chunk=0, score=None):
    from langchain_core.documents import Document

    meta = {
        "source": source, "dept": "Tech", "kind": "md", "page": page,
        "slide": 0, "sheet": "", "title": "", "chunk_index": chunk,
    }
    if score is not None:
        meta["retrieval_score"] = score
    return Document(page_content=text, metadata=meta)


class StubLLM:
    def invoke(self, _messages):
        from langchain_core.messages import AIMessage

        return AIMessage(content="ok")


def _run_agent(docs, monkeypatch):
    class Engine:
        @property
        def embeddings(self):
            raise AssertionError("not used")

        def search(self, *_args, **_kwargs):
            return docs

    monkeypatch.setattr(agent_module, "get_engine", lambda: Engine())
    monkeypatch.setattr(agent_module, "llm", StubLLM())
    return agent_module.call_model(
        {
            "messages": [HumanMessage(content="q")],
            "user_id": "u1",
            "user_dept": "Tech",
            "user_depts": ["Tech"],
        }
    )


def test_same_page_chunks_are_merged(monkeypatch):
    docs = [
        _doc("first chunk text", source="Tech/a.md", page=1, chunk=0, score=0.9),
        _doc("second much longer chunk text", source="Tech/a.md", page=1, chunk=1, score=0.5),
        _doc("other page", source="Tech/a.md", page=2, chunk=0, score=0.7),
    ]
    update = _run_agent(docs, monkeypatch)
    sources = update["sources"]
    assert len(sources) == 2
    merged = next(s for s in sources if s.get("page") == 1)
    assert "second much longer" in merged["snippet"]
    assert merged["score"] == 1.0
    assert sources[0]["score"] >= sources[-1]["score"]


def test_scores_are_relative_ranks(monkeypatch):
    docs = [
        _doc(source="Tech/a.md", page=1, score=0.001),
        _doc(source="Tech/b.md", page=1, score=0.9),
        _doc(source="Tech/c.md", page=1, score=0.01),
    ]
    sources = _run_agent(docs, monkeypatch)["sources"]
    assert [s["source"] for s in sources] == ["Tech/b.md", "Tech/c.md", "Tech/a.md"]
    assert sources[0]["score"] == 1.0
    assert sources[-1]["score"] < sources[0]["score"]


def test_sources_without_scores_keep_order(monkeypatch):
    docs = [_doc(source="Tech/a.md", page=1), _doc(source="Tech/b.md", page=1)]
    sources = _run_agent(docs, monkeypatch)["sources"]
    assert "score" not in sources[0]


def test_l2_to_score_is_monotonic():
    assert _l2_to_score(0.0) == 1.0
    assert _l2_to_score(0.1) > _l2_to_score(1.0)
    assert _l2_to_score("bad") == 0.0


def test_hits_to_documents_distance_mode():
    hits = [{"distance": 0.25, "entity": {"text": "a", "source": "x"}}]
    docs = hits_to_documents(hits, score_mode="distance")
    assert docs[0].metadata["retrieval_score"] == pytest.approx(0.8)


def test_hits_to_documents_score_mode():
    hits = [{"distance": 0.42, "entity": {"text": "a", "source": "x"}}]
    docs = hits_to_documents(hits, score_mode="score")
    assert docs[0].metadata["retrieval_score"] == 0.42


def test_rerank_ms_is_recorded(monkeypatch):
    class Engine:
        @property
        def embeddings(self):
            raise AssertionError("not used")

        def search(self, _query, _depts, k=4, timings=None):
            docs = [_doc(score=0.5)]
            if timings is not None:
                timings["rerank_ms"] = timings.get("rerank_ms", 0.0) + 12.5
            return docs

    monkeypatch.setattr(agent_module, "get_engine", lambda: Engine())
    monkeypatch.setattr(agent_module, "llm", StubLLM())
    update = agent_module.call_model(
        {
            "messages": [HumanMessage(content="q")],
            "user_id": "u1",
            "user_dept": "Tech",
            "user_depts": ["Tech"],
        }
    )
    assert update["timings"]["rerank_ms"] == 12.5
