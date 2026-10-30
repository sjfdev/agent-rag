"""Prometheus metrics wiring."""


from src import metrics


def test_observe_request_records_timings():
    metrics.observe_request(
        endpoint="test_chat",
        status="ok",
        elapsed_seconds=0.5,
        timings={"retrieval_ms": 20, "llm_ms": 400, "llm_calls": 2},
    )
    text = metrics.render().decode()
    assert 'agent_rag_chat_requests_total{endpoint="test_chat",status="ok"}' in text
    assert 'agent_rag_llm_calls_total{endpoint="test_chat"}' in text


def test_observe_request_without_timings():
    metrics.observe_request(endpoint="test_plain", status="error", elapsed_seconds=0.1)
    text = metrics.render().decode()
    assert 'agent_rag_chat_requests_total{endpoint="test_plain",status="error"}' in text


def test_cache_and_retrieval_counters():
    metrics.observe_cache("answer", "hit")
    metrics.observe_cache("embedding", "miss")
    metrics.observe_retrieval("milvus")
    metrics.observe_first_token("chat_stream", 0.25)
    metrics.observe_ingest_job("done")
    text = metrics.render().decode()
    assert 'agent_rag_cache_total{cache="answer",result="hit"}' in text
    assert 'agent_rag_retrieval_engine_total{engine="milvus"}' in text
    assert "agent_rag_first_token_seconds_count" in text
    assert 'agent_rag_ingest_jobs_total{status="done"}' in text


def test_set_hybrid_and_maintenance(monkeypatch):
    # render() refreshes the hybrid gauge from the engine, so stub the engine.
    import src.engine as engine_module

    class Local:
        def live_count(self):
            return 1

        def hybrid_status(self):
            return {"engine": "local", "sparse_ready": False}

        def cache_status(self):
            return {"embeddings": {"entries": 0}}

    monkeypatch.setattr(engine_module, "kb_engine", Local())
    text = metrics.render().decode()
    assert 'agent_rag_hybrid_engine{engine="local"} 1.0' in text
    assert 'agent_rag_hybrid_engine{engine="milvus"} 0.0' in text
    assert "agent_rag_hybrid_sparse_ready 0.0" in text

    # render() derives maintenance from the runtime flag, not the raw gauge.
    from src import runtime

    runtime.begin_maintenance()
    try:
        assert "agent_rag_maintenance 1.0" in metrics.render().decode()
    finally:
        runtime.end_maintenance()
    assert "agent_rag_maintenance 0.0" in metrics.render().decode()


def test_refresh_index_gauge_reads_live_count(monkeypatch):
    import src.engine as engine_module

    class FakeEngine:
        def live_count(self):
            return 7

        def hybrid_status(self):
            return {"engine": "local", "sparse_ready": False}

        def cache_status(self):
            return {"embeddings": {"entries": 1}}

    monkeypatch.setattr(engine_module, "kb_engine", FakeEngine())
    metrics._index_state["at"] = 0.0
    text = metrics.render().decode()
    assert "agent_rag_index_chunks 7.0" in text
    assert 'agent_rag_cache_entries{cache="embedding"} 1.0' in text


def test_render_survives_engine_errors(monkeypatch):
    import src.engine as engine_module

    class Broken:
        def live_count(self):
            raise RuntimeError("store down")

        def hybrid_status(self):
            raise RuntimeError("store down")

        def cache_status(self):
            raise RuntimeError("store down")

    monkeypatch.setattr(engine_module, "kb_engine", Broken())
    metrics._index_state["at"] = 0.0
    text = metrics.render().decode()
    assert "agent_rag_chat_requests_total" in text


def test_index_gauge_is_throttled(monkeypatch):
    import src.engine as engine_module

    calls = {"n": 0}

    class Counting:
        def live_count(self):
            calls["n"] += 1
            return 3

        def hybrid_status(self):
            return {"engine": "local", "sparse_ready": False}

        def cache_status(self):
            return {"embeddings": {"entries": 0}}

    monkeypatch.setattr(engine_module, "kb_engine", Counting())
    metrics._index_state["at"] = metrics.time.monotonic()
    metrics.render()
    metrics.render()
    assert calls["n"] == 0
