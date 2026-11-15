"""Admin vector-store management: stats, chunk browsing, search, pruning.

管理台向量库管理接口测试。
"""

import hashlib
import importlib

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.etl import doc_id_for
from src.hashing import file_sha1


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "DATA_DIR", str(data))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(settings, "JOBS_PATH", str(tmp_path / "jobs.json"))

    from src.engine import get_engine, reset_engine

    reset_engine()
    yield TestClient, tmp_path, data, get_engine()
    reset_engine()


def _meta(dept="Tech", source="Tech/a.md", doc_id="d1", doc_hash="h1", chunk=0):
    return {
        "dept": dept,
        "source": source,
        "doc_id": doc_id,
        "doc_hash": doc_hash,
        "kind": "md",
        "page": 1,
        "slide": 0,
        "sheet": "",
        "title": "",
        "chunk_index": chunk,
    }


def _seed_indexed(client_fixture, texts=("alpha chunk", "beta chunk")):
    _, _, data, engine = client_fixture
    path = data / "Tech" / "a.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("alpha chunk\nbeta chunk\n")
    doc_id = doc_id_for("Tech", "Tech/a.md")
    doc_hash = file_sha1(path)
    engine.add_documents(
        list(texts),
        [
            _meta(doc_id=doc_id, doc_hash=doc_hash, chunk=index)
            for index in range(len(texts))
        ],
    )
    return doc_id


def _orphan_id():
    return hashlib.sha1(b"missing|Ghost/gone.md").hexdigest()


def _app(client_fixture):
    TestClientCls = client_fixture[0]
    import main

    importlib.reload(main)
    return TestClientCls(main.app)


def test_stats_and_documents(client):
    doc_id = _seed_indexed(client)
    with _app(client) as test_client:
        body = test_client.get("/admin/api/vectors").json()
        stats = body["stats"]
        assert stats["backend"] == "faiss"
        assert stats["chunks"] == 2
        assert stats["live_chunks"] == 2
        assert stats["documents"] == 1
        assert stats["orphans"] == 0
        assert stats["depts"] == ["Tech"]
        assert stats["hybrid"]["enabled"] is True

        entries = body["documents"]
        assert len(entries) == 1
        assert entries[0]["doc_id"] == doc_id
        assert entries[0]["source"] == "Tech/a.md"
        assert entries[0]["chunks"] == 2
        assert entries[0]["status"] == "indexed"
        assert entries[0]["ext"] == ".md"
        assert entries[0]["size"] and entries[0]["mtime"]
        assert entries[0]["exists"] is True


def test_orphans_are_reported_and_filterable(client):
    _seed_indexed(client)
    _, _, _, engine = client
    orphan = _orphan_id()
    engine.add_documents(["lost text"], [_meta(dept="Ghost", source="Ghost/gone.md", doc_id=orphan)])

    with _app(client) as test_client:
        body = test_client.get("/admin/api/vectors").json()
        assert body["stats"]["orphans"] == 1
        assert body["stats"]["documents"] == 2
        orphans = test_client.get("/admin/api/vectors?status=orphan").json()["documents"]
        assert [item["doc_id"] for item in orphans] == [orphan]
        assert orphans[0]["dept"] == "Ghost"
        assert orphans[0]["exists"] is False
        assert test_client.get("/admin/api/vectors?dept=Ghost").json()["documents"][0][
            "doc_id"
        ] == orphan
        assert test_client.get("/admin/api/vectors?status=bogus").status_code == 400


def test_chunk_browser_pages_and_truncates(client):
    doc_id = _seed_indexed(client, texts=("alpha " * 400, "beta"))
    _, _, _, engine = client
    with _app(client) as test_client:
        body = test_client.get(f"/admin/api/vectors/chunks?doc_id={doc_id}").json()
        assert body["total"] == 2
        assert [chunk["chunk_index"] for chunk in body["chunks"]] == [0, 1]
        assert body["chunks"][0]["truncated"] is True
        assert len(body["chunks"][0]["text"]) == 2000

        page = test_client.get(
            f"/admin/api/vectors/chunks?doc_id={doc_id}&offset=1&limit=1"
        ).json()
        assert page["total"] == 2
        assert [chunk["chunk_index"] for chunk in page["chunks"]] == [1]

        bad = test_client.get("/admin/api/vectors/chunks?doc_id=nothex")
        assert bad.status_code == 400
        assert engine.count_by_doc_id(doc_id) == 2


def test_search_is_department_scoped(client):
    _seed_indexed(client)
    with _app(client) as test_client:
        body = test_client.post(
            "/admin/api/vectors/search",
            json={"dept": "Tech", "query": "alpha", "k": 3},
        ).json()
        assert body["depts"] == "Tech"
        assert body["results"]
        assert body["results"][0]["source"] == "Tech/a.md"
        assert "score" in body["results"][0]

        assert (
            test_client.post(
                "/admin/api/vectors/search", json={"dept": "Nope", "query": "alpha"}
            ).status_code
            == 404
        )
        assert (
            test_client.post(
                "/admin/api/vectors/search", json={"dept": "Tech", "query": ""}
            ).status_code
            == 422
        )


def test_delete_vectors_keeps_the_file(client):
    doc_id = _seed_indexed(client)
    _, _, data, _ = client
    with _app(client) as test_client:
        resp = test_client.delete(f"/admin/api/vectors/{doc_id}")
        assert resp.status_code == 200
        assert resp.json()["chunks_removed"] == 2
        assert (data / "Tech" / "a.md").exists()
        entries = test_client.get("/admin/api/vectors").json()["documents"]
        assert entries[0]["chunks"] == 0
        assert entries[0]["status"] == "pending"
        audit = test_client.get("/admin/api/audit?limit=5").json()["records"]
        assert any(record["action"] == "vector.delete" for record in audit)
        assert test_client.delete("/admin/api/vectors/nothex").status_code == 400


def test_prune_removes_orphans(client):
    _seed_indexed(client)
    _, _, _, engine = client
    orphan = _orphan_id()
    engine.add_documents(["lost"], [_meta(dept="Ghost", source="Ghost/gone.md", doc_id=orphan)])
    with _app(client) as test_client:
        resp = test_client.post("/admin/api/vectors/prune")
        assert resp.status_code == 200
        assert resp.json() == {"documents_removed": 1, "chunks_removed": 1}
        assert test_client.get("/admin/api/vectors").json()["stats"]["orphans"] == 0
        audit = test_client.get("/admin/api/audit?limit=5").json()["records"]
        assert any(record["action"] == "vector.prune" for record in audit)


def test_writes_are_rejected_while_ingesting(client):
    doc_id = _seed_indexed(client)
    from src import admin_api

    assert admin_api._ingest_lock.acquire(blocking=False)
    try:
        with _app(client) as test_client:
            assert test_client.delete(f"/admin/api/vectors/{doc_id}").status_code == 409
            assert test_client.post("/admin/api/vectors/prune").status_code == 409
    finally:
        admin_api._ingest_lock.release()
