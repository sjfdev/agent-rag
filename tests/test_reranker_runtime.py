"""Reranker fallbacks, runtime maintenance flags and the tool.

`src.reranker` imports sentence-transformers lazily, so these tests inject a
fake module into `sys.modules` instead of pulling torch into the CI image.
"""

import sys
import types

import pytest
from langchain_core.documents import Document

from src import runtime
from src.config import settings
from src.reranker import EnterpriseReranker, rerank_documents
from src.tools import create_support_ticket


def _docs():
    return [
        Document(page_content="low", metadata={"source": "a"}),
        Document(page_content="high", metadata={"source": "b"}),
    ]


class _FakeCrossEncoder:
    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def predict(self, pairs, batch_size=8):
        self.calls += 1
        return self.scores


@pytest.fixture()
def fake_st(monkeypatch):
    """Install a stub `sentence_transformers` module for the import in reranker."""
    module = types.ModuleType("sentence_transformers")
    module.CrossEncoder = lambda name: _FakeCrossEncoder([0.1, 0.9])
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return module


def test_reranker_sorts_by_score(fake_st):
    fake_st.CrossEncoder = lambda name: _FakeCrossEncoder([0.1, 0.9])
    reranker = EnterpriseReranker("fake")
    ranked = reranker.rerank("q", _docs(), top_k=1)
    assert ranked and ranked[0].metadata["source"] == "b"


def test_reranker_returns_single_doc_unchanged():
    reranker = EnterpriseReranker("fake")
    docs = _docs()[:1]
    assert reranker.rerank("q", docs, 3) == docs


def test_reranker_load_failure_degrades(fake_st):
    def boom(_name):
        raise OSError("no cache")

    fake_st.CrossEncoder = boom
    reranker = EnterpriseReranker("missing")
    docs = _docs()
    assert reranker.rerank("q", docs, 2) == docs
    assert reranker._failed is True
    # Second call does not retry loading.
    assert reranker.rerank("q", docs, 2) == docs


def test_reranker_inference_failure_degrades(fake_st):
    class Bad(_FakeCrossEncoder):
        def predict(self, pairs, batch_size=8):
            raise RuntimeError("oom")

    fake_st.CrossEncoder = lambda name: Bad([])
    reranker = EnterpriseReranker("fake")
    docs = _docs()
    assert reranker.rerank("q", docs, 2) == docs


def test_rerank_documents_respects_flag(monkeypatch, fake_st):
    docs = _docs()
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    assert rerank_documents("q", docs, 1) == docs[:1]

    fake_st.CrossEncoder = lambda name: _FakeCrossEncoder([0.2, 0.8])
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    ranked = rerank_documents("q", docs, 1)
    assert ranked[0].metadata["source"] == "b"


def test_maintenance_flag_roundtrip(monkeypatch):
    runtime.end_maintenance()
    assert runtime.in_maintenance() is False
    runtime.begin_maintenance()
    assert runtime.in_maintenance() is True
    runtime.end_maintenance()
    assert runtime.in_maintenance() is False


def test_maintenance_flag_survives_metrics_failure(monkeypatch):
    import src.metrics

    def boom(_active):
        raise RuntimeError("metrics broken")

    monkeypatch.setattr(src.metrics, "set_maintenance", boom)
    runtime.begin_maintenance()
    try:
        assert runtime.in_maintenance() is True
    finally:
        runtime.end_maintenance()


def test_support_ticket_uses_session_user():
    result = create_support_ticket.invoke(
        {"issue": "laptop broken", "state": {"user_id": "u42"}}
    )
    assert "TICK-" in result
    assert "u42" in result
