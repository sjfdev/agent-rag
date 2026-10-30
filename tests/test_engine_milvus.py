"""Milvus driver branches exercised with an in-memory fake client.

No Milvus server is required: the driver talks to its client through a small
duck-typed stub, so hybrid search, counting, deletion and rebuild switching can
be tested offline.
"""

import pytest

from src.config import settings
from src.engine import FakeEmbeddings, MilvusCapabilities, MilvusDriver


class FakeIterator:
    def __init__(self, rows):
        self._rows = list(rows)
        self.closed = False

    def next(self):
        if not self._rows:
            return []
        return self._rows.pop(0)

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, rows=None, aliases=None, collections=None):
        self.rows = rows or []
        self.aliases = list(aliases or [])
        self.collections = set(collections or ["enterprise_kb"])
        self.missing = set()
        self.deleted = []
        self.flushed = []
        self.search_calls = 0
        self.hybrid_calls = 0
        self.fail_search = False
        self.fail_hybrid = False

    # metadata
    def has_collection(self, name):
        if name in self.missing:
            return False
        return name in self.collections or name.startswith("enterprise_kb__")

    def list_collections(self):
        return sorted(self.collections)

    def list_indexes(self, name):
        return ["vector"]

    def describe_index(self, name, index):
        return {"field_name": "vector", "metric_type": "L2"}

    def describe_collection(self, name):
        return {"collection_name": name, "fields": []}

    def get_server_version(self):
        return "v2.5.4"

    def close(self):
        pass

    # queries
    def query(self, collection_name, filter="", output_fields=None, limit=None):
        if "count(*)" in (output_fields or []):
            picked = self.rows
            if filter and "doc_id ==" in filter:
                doc_id = filter.split('"')[1]
                picked = [r for r in picked if r.get("doc_id") == doc_id]
            return [{"count(*)": len(picked)}]
        picked = self.rows
        if filter and "doc_id ==" in filter:
            doc_id = filter.split('"')[1]
            picked = [r for r in self.rows if r.get("doc_id") == doc_id]
        return picked[: (limit or len(picked))]

    def query_iterator(self, collection_name, batch_size=1000, filter="", output_fields=None):
        return FakeIterator([list(self.rows)])

    # writes
    def delete(self, collection_name, expr=None, **kwargs):
        self.deleted.append(expr)

    def flush(self, name):
        self.flushed.append(name)

    def search(self, collection_name, data, anns_field, search_params, limit, filter, output_fields):
        self.search_calls += 1
        if self.fail_search:
            raise RuntimeError("search boom")
        return [[{"entity": {"text": "alpha", "source": "Tech/a.md", "dept": "Tech"}}]]

    def hybrid_search(self, collection_name, reqs, ranker, limit, output_fields):
        self.hybrid_calls += 1
        if self.fail_hybrid:
            raise RuntimeError("hybrid boom")
        return [[{"entity": {"text": "alpha", "source": "Tech/a.md", "dept": "Tech"}}]]

    # aliases / collections
    def create_alias(self, collection_name, alias):
        if alias in self.aliases:
            raise RuntimeError("alias exists")
        self.aliases.append(alias)

    def alter_alias(self, collection_name, alias):
        if alias not in self.aliases:
            raise RuntimeError("no alias")

    def drop_alias(self, alias):
        self.aliases.remove(alias)

    def describe_alias(self, alias):
        return {"alias": alias, "collection_name": f"{alias}__prev"}

    def list_aliases(self):
        return {"aliases": list(self.aliases), "db_name": "default"}

    def rename_collection(self, old, new):
        self.collections.discard(old)
        self.collections.add(new)

    def drop_collection(self, name):
        self.collections.discard(name)


class FakeVectorDB:
    def __init__(self, client, fields=None):
        self.client = client
        self.fields = fields or ["text", "vector", "dept"]
        self.added = []
        self.deleted = []

    def add_documents(self, docs):
        self.added.extend(docs)

    def delete(self, expr=None, **kwargs):
        self.deleted.append(expr)

    def similarity_search(self, query, k=4, expr=None):
        from langchain_core.documents import Document

        return [Document(page_content="fallback", metadata={"source": "x", "dept": "Tech"})]


def make_driver(client=None, sparse=True, capabilities=None, fields=None):
    client = client or FakeClient()
    driver = MilvusDriver.__new__(MilvusDriver)
    driver.embeddings = FakeEmbeddings()
    driver.collection = "enterprise_kb"
    driver.sparse_enabled = sparse
    driver.capabilities = capabilities or MilvusCapabilities(
        server_version="v2.5.4", version_ok=True,
        collection_exists=True, sparse_field=True, bm25_index=True,
    )
    driver.vector_db = FakeVectorDB(client, fields)
    return driver


def test_write_target_defaults_to_live():
    driver = make_driver()
    assert driver._write_collection == "enterprise_kb"
    assert driver._write_db is driver.vector_db


def test_write_target_switches_to_shadow():
    driver = make_driver()
    driver._shadow_name = "shadow"
    driver._rebuild_target = FakeVectorDB(FakeClient())
    assert driver._write_collection == "shadow"
    assert driver._write_db is driver._rebuild_target


def test_hybrid_search_returns_documents():
    driver = make_driver()
    docs = driver.hybrid_search("q", "Tech", 3, 5)
    assert docs and docs[0].page_content == "alpha"
    assert driver.vector_db.client.hybrid_calls == 1


def test_hybrid_search_disabled_returns_none():
    driver = make_driver(sparse=False)
    assert driver.hybrid_search("q", "Tech", 3, 5) is None


def test_hybrid_search_failure_returns_none():
    client = FakeClient()
    client.fail_hybrid = True
    driver = make_driver(client)
    assert driver.hybrid_search("q", "Tech", 3, 5) is None


def test_dense_search_uses_explicit_vector_field():
    driver = make_driver(fields=["text", "vector", "dept", "sparse"])
    docs = driver.search("q", "Tech", 3)
    assert docs and docs[0].page_content == "alpha"
    assert driver.vector_db.client.search_calls == 1


def test_dense_search_failure_returns_empty():
    client = FakeClient()
    client.fail_search = True
    driver = make_driver(client, fields=["sparse"])
    assert driver.search("q", "Tech", 3) == []


def test_plain_similarity_search_without_sparse():
    driver = make_driver(sparse=False, fields=["text", "vector"])
    docs = driver.search("q", "Tech", 3)
    assert docs[0].page_content == "fallback"


def test_count_and_live_count():
    client = FakeClient(rows=[{"doc_id": "a"}, {"doc_id": "b"}])
    driver = make_driver(client)
    assert driver.count() == 2
    assert driver.count_live() == 2
    assert driver.is_empty() is False


def test_count_missing_collection_is_zero():
    client = FakeClient(rows=[])

    def missing(*args, **kwargs):
        raise RuntimeError("collection not found")

    client.query = missing
    driver = make_driver(client)
    assert driver.count() == 0


def test_count_propagates_other_errors():
    client = FakeClient()

    def broken(*args, **kwargs):
        raise RuntimeError("connection refused")

    client.query = broken
    driver = make_driver(client)
    with pytest.raises(RuntimeError):
        driver.count()


def test_count_in_shadow():
    client = FakeClient(rows=[{"doc_id": "a"}])
    driver = make_driver(client)
    driver._shadow_name = "enterprise_kb__shadow"
    driver._rebuild_target = FakeVectorDB(client)
    assert driver.count() == 1


def test_doc_summary_aggregates_chunks():
    client = FakeClient(
        rows=[
            {"doc_id": "a", "doc_hash": "h1"},
            {"doc_id": "a", "doc_hash": "h1"},
            {"doc_id": "b", "doc_hash": "h2"},
        ]
    )
    driver = make_driver(client)
    summary = driver.doc_summary()
    assert summary["a"] == {"doc_hash": "h1", "chunks": 2}
    assert summary["b"]["chunks"] == 1
    assert driver.list_doc_ids() == {"a", "b"}


def test_doc_summary_missing_collection_is_empty():
    client = FakeClient()
    client.missing.add("enterprise_kb")
    driver = make_driver(client)
    assert driver.doc_summary() == {}


def test_doc_summary_failure_is_empty():
    client = FakeClient()
    client.query_iterator = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    driver = make_driver(client)
    assert driver.doc_summary() == {}


def test_list_depts_ignores_errors():
    client = FakeClient(rows=[{"dept": "Tech"}, {"dept": "HR"}])
    driver = make_driver(client)
    assert driver.list_depts() == {"Tech", "HR"}
    client.query = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    assert driver.list_depts() == set()


def test_get_doc_hash_reads_shadow_during_rebuild():
    client = FakeClient(rows=[{"doc_id": "a", "doc_hash": "h1"}])
    driver = make_driver(client)
    assert driver.get_doc_hash("a") == "h1"
    assert driver.get_doc_hash("missing") is None
    driver._shadow_name = "enterprise_kb__shadow"
    driver._rebuild_target = FakeVectorDB(client)
    assert driver.get_doc_hash("a") == "h1"


def test_delete_by_doc_ids_batches_and_flushes():
    client = FakeClient(rows=[{"doc_id": f"d{i}"} for i in range(3)])
    driver = make_driver(client)
    removed = driver.delete_by_doc_ids(["d0", "d1", "d2"])
    assert removed == 3
    assert driver.vector_db.client.flushed
    assert any("doc_id in" in (expr or "") for expr in driver.vector_db.deleted)


def test_delete_by_doc_ids_empty():
    driver = make_driver()
    assert driver.delete_by_doc_ids([]) == 0


def test_delete_by_doc_ids_failure_is_swallowed():
    client = FakeClient()

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    client.query = broken
    driver = make_driver(client)
    assert driver.delete_by_doc_ids(["d1"]) == 0


def test_count_by_doc_id():
    client = FakeClient(rows=[{"doc_id": "a"}, {"doc_id": "a"}])
    driver = make_driver(client)
    assert driver.count_by_doc_id("a") == 2
    assert driver.count_by_doc_id("") == 0
    client.query = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    assert driver.count_by_doc_id("a") == 0


def test_delete_stale_versions_and_expr():
    client = FakeClient(rows=[{"doc_id": "a"}])
    driver = make_driver(client)
    removed = driver.delete_stale_versions("a", "newhash")
    assert removed == 1
    assert driver.delete_stale_versions("", "x") == 0
    assert driver.delete_stale_versions("a", "") == 0
    assert any("doc_hash !=" in (expr or "") for expr in driver.vector_db.deleted)


def test_delete_by_expr_swallows_write_failure():
    client = FakeClient(rows=[{"doc_id": "a"}])
    db = FakeVectorDB(client)

    def broken(expr=None, **kwargs):
        raise RuntimeError("delete boom")

    db.delete = broken
    driver = make_driver(client)
    driver.vector_db = db
    assert driver.delete_by_expr('doc_id == "a"') == 0


def test_fetch_documents_filters_rows():
    client = FakeClient(
        rows=[{"text": "alpha", "source": "Tech/a.md", "dept": "Tech"}]
    )
    driver = make_driver(client)
    docs = driver.fetch_documents("Tech")
    assert docs and docs[0].page_content == "alpha"
    assert "text" not in docs[0].metadata


def test_fetch_documents_failure_is_empty():
    client = FakeClient()
    client.query_iterator = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    driver = make_driver(client)
    assert driver.fetch_documents("Tech") == []


def test_add_documents_writes_to_shadow_and_refreshes(monkeypatch):
    client = FakeClient()
    driver = make_driver(client)
    calls = {"n": 0}
    monkeypatch.setattr(driver, "refresh_capabilities", lambda: calls.__setitem__("n", calls["n"] + 1))
    driver.add_documents(["text"], [{"dept": "Tech"}])
    assert driver.vector_db.added
    assert calls["n"] == 1

    shadow_db = FakeVectorDB(client)
    driver._shadow_name = "shadow"
    driver._rebuild_target = shadow_db
    driver.add_documents(["text2"], [{"dept": "Tech"}])
    assert shadow_db.added
    assert calls["n"] == 1  # no refresh while rebuilding


def test_refresh_capabilities_reprobes(monkeypatch):
    driver = make_driver()
    monkeypatch.setattr(
        "src.engine._inspect_milvus",
        lambda *args: MilvusCapabilities(server_version="v2.5.5", version_ok=True),
    )
    driver.refresh_capabilities()
    assert driver.capabilities.server_version == "v2.5.5"


def test_begin_and_abort_rebuild(monkeypatch):
    client = FakeClient()
    driver = make_driver(client)
    fake_shadow = FakeVectorDB(client)
    monkeypatch.setattr(driver, "_open_collection", lambda name, create=False: fake_shadow)
    driver.begin_rebuild()
    assert driver._rebuild_target is fake_shadow
    assert driver.shadow_collection.startswith("enterprise_kb__r")
    driver.abort_rebuild()
    assert driver._rebuild_target is None
    assert driver.shadow_collection.startswith("enterprise_kb__rebuild")


def test_commit_rebuild_creates_alias_and_drops_previous(monkeypatch):
    client = FakeClient(collections=["enterprise_kb", "shadow"])
    driver = make_driver(client)
    live = FakeVectorDB(client)
    monkeypatch.setattr(driver, "_open_collection", lambda name, create=False: live)

    # Simulate an existing physical collection with the live name.
    client.aliases = []
    driver._shadow_name = "shadow"
    driver._rebuild_target = FakeVectorDB(client)
    driver.commit_rebuild()
    assert "enterprise_kb" in client.aliases
    assert driver._rebuild_target is None
    assert driver.collection == "enterprise_kb"


def test_commit_rebuild_requires_shadow():
    client = FakeClient()
    driver = make_driver(client)
    driver._shadow_name = "missing"
    with pytest.raises(RuntimeError):
        driver.commit_rebuild()


def test_commit_rebuild_replaces_existing_alias(monkeypatch):
    client = FakeClient(collections=["shadow"])
    client.aliases = ["enterprise_kb"]
    driver = make_driver(client)
    monkeypatch.setattr(driver, "_open_collection", lambda name, create=False: FakeVectorDB(client))
    driver._shadow_name = "shadow"
    driver._rebuild_target = FakeVectorDB(client)
    driver.commit_rebuild()
    assert "enterprise_kb" in client.aliases


def test_drop_legacy_collection():
    client = FakeClient(collections=["enterprise_kb", "legacy"])
    driver = make_driver(client)
    driver._legacy_collection = "legacy"
    driver.drop_legacy_collection()
    assert "legacy" not in client.collections
    assert driver._legacy_collection is None


def test_reset_reinitialises(monkeypatch):
    client = FakeClient(collections=["enterprise_kb"])
    driver = make_driver(client)
    called = {"n": 0}
    monkeypatch.setattr(
        MilvusDriver,
        "__init__",
        lambda self, embeddings: called.__setitem__("n", called["n"] + 1),
    )
    driver.reset()
    assert called["n"] == 1
    assert "enterprise_kb" not in client.collections


def test_decide_sparse_auto_logs_fallback(monkeypatch):
    driver = make_driver(
        capabilities=MilvusCapabilities(server_version="v2.4.0", version_ok=False)
    )
    monkeypatch.setattr(settings, "HYBRID_ENABLED", True)
    monkeypatch.setattr(settings, "HYBRID_ENGINE", "auto")
    assert driver._decide_sparse() is False
