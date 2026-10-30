"""Small uncovered branches: hybrid helpers, loaders helpers, misc."""

import pytest

from src import hybrid
from src.config import settings
from src.engine import _inspect_milvus
from src.loaders import read_text


def test_connection_args_uri(monkeypatch):
    monkeypatch.setattr(settings, "MILVUS_URI", "http://example:19530")
    assert hybrid.connection_args() == {"uri": "http://example:19530"}


def test_connection_args_host_port(monkeypatch):
    monkeypatch.setattr(settings, "MILVUS_URI", "")
    monkeypatch.setattr(settings, "MILVUS_HOST", "h")
    monkeypatch.setattr(settings, "MILVUS_PORT", "1")
    assert hybrid.connection_args() == {"host": "h", "port": "1"}


def test_version_at_least_with_unparseable_minimum():
    assert hybrid.version_at_least("v2.5.4", "not-a-version") is False


def test_resolve_rejects_unknown_mode():
    with pytest.raises(ValueError):
        hybrid.resolve_hybrid_mode("nope", True, True)


def test_inspect_milvus_handles_connection_failure(monkeypatch):
    class BrokenClient:
        def __init__(self, **kwargs):
            raise ConnectionError("down")

    import pymilvus

    monkeypatch.setattr(pymilvus, "MilvusClient", BrokenClient)
    caps = _inspect_milvus("enterprise_kb", "sparse")
    assert caps.version_ok is False
    assert caps.error and "ConnectionError" in caps.error


def test_read_text_falls_back_to_chardet(tmp_path):
    # GB18030 text decodes with the built-in list, so force the fallback via a
    # file whose bytes are not valid in any listed encoding.
    path = tmp_path / "weird.txt"
    path.write_bytes(b"\xff\xfe\x00\x41")
    text = read_text(path)
    assert isinstance(text, str)


def test_json_loader_handles_invalid_json(tmp_path):
    from src.loaders import load_file

    path = tmp_path / "bad.json"
    path.write_text("not json at all")
    docs = load_file(path)
    assert docs and docs[0].metadata["kind"] == "json"


def test_csv_loader_without_sniffable_delimiter(tmp_path):
    from src.loaders import load_file

    path = tmp_path / "plain.csv"
    path.write_text("single line no delimiter")
    docs = load_file(path)
    assert docs and "single line" in docs[0].page_content


def test_xlsx_truncation_warns(tmp_path, monkeypatch):
    from openpyxl import Workbook

    import src.loaders as loaders

    monkeypatch.setattr(loaders, "MAX_ROWS_PER_SHEET", 2)
    path = tmp_path / "big.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    for i in range(5):
        ws.append([f"row{i}", i])
    wb.save(path)
    docs = loaders.load_file(path)
    assert docs and docs[0].metadata["sheet"] == "Data"


def test_audit_tail_skips_corrupt_lines(tmp_path, monkeypatch):
    from src import audit

    path = tmp_path / "audit.log"
    path.write_text('{"action": "ok", "ts": "t"}\nnot-json\n{"action": "ok2", "ts": "t"}\n')
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(path))
    records = audit.tail()
    assert [r["action"] for r in records] == ["ok", "ok2"]


def test_job_store_without_persistence(monkeypatch):
    from src.jobs import JobStore

    monkeypatch.setattr(settings, "JOBS_PATH", "")
    store = JobStore()
    store.create({"id": "a", "status": "queued", "created": 1, "log": []})
    assert store.get("a")["status"] == "queued"
    assert store.list()[0]["id"] == "a"


def test_embedding_cache_clear_and_proxy(monkeypatch):
    from src.embedding_cache import CachedEmbeddings

    class Inner:
        def embed_query(self, text):
            return [1.0]

        def embed_documents(self, texts):
            return [[1.0] for _ in texts]

    cache = CachedEmbeddings(Inner(), namespace="n", max_entries=1)
    cache.embed_query("a")
    cache.embed_query("b")
    assert cache.stats()["entries"] == 1
    cache.clear()
    assert cache.stats()["entries"] == 0


def test_chat_service_dunder_attribute_guard():
    from src.engine import _LazyEngine

    proxy = _LazyEngine()
    with pytest.raises(AttributeError):
        # Frameworks probe dunder attributes during introspection.
        proxy.__wrapped__  # noqa: B018
