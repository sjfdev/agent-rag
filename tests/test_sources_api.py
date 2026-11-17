"""Citation preview: full chunk text plus department and ACL isolation."""

import json

import pytest
from fastapi.testclient import TestClient

from src import acl
from src.config import settings

LONG_TEXT = (
    "Every change must go through a pull request with one approving reviewer. "
    "The reviewer must be from a different team than the author, and the pull "
    "request description has to list the tests that were run before merging. "
    "This sentence is long enough that the citation snippet would be truncated."
)


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    from src.engine import kb_engine as engine

    data = tmp_path / "data"
    (data / "Tech").mkdir(parents=True)
    (data / "Finance").mkdir(parents=True)
    (data / "Tech" / "policy.md").write_text(LONG_TEXT, encoding="utf-8")
    (data / "Finance" / "budget.md").write_text("budget bytes", encoding="utf-8")
    monkeypatch.setattr(settings, "DATA_DIR", str(data))

    engine.reset()
    engine.add_documents(
        [
            "Short first chunk.",
            LONG_TEXT,
            "Second page content.",
            "Salaries are confidential and only visible to Finance.",
        ],
        [
            {"dept": "Tech", "source": "Tech/policy.md", "doc_id": "tech1",
             "doc_hash": "h1", "kind": "md", "page": 1, "slide": 0, "sheet": "",
             "title": "Policy", "chunk_index": 0},
            {"dept": "Tech", "source": "Tech/policy.md", "doc_id": "tech1",
             "doc_hash": "h1", "kind": "md", "page": 1, "slide": 0, "sheet": "",
             "title": "Policy", "chunk_index": 1},
            {"dept": "Tech", "source": "Tech/policy.md", "doc_id": "tech1",
             "doc_hash": "h1", "kind": "md", "page": 2, "slide": 0, "sheet": "",
             "title": "Policy", "chunk_index": 2},
            {"dept": "Finance", "source": "Finance/budget.md", "doc_id": "fin1",
             "doc_hash": "h2", "kind": "md", "page": 1, "slide": 0, "sheet": "",
             "title": "", "chunk_index": 0},
        ],
    )
    import main

    with TestClient(main.app) as client:
        try:
            yield client, engine
        finally:
            engine.reset()


def _preview(client, source, dept="Tech", **params):
    query = {"source": source, "user_id": "u1", **params}
    if dept is not None:
        query["user_dept"] = dept
    return client.get("/sources/content", params=query)


def test_preview_returns_the_full_chunk_text(seeded):
    client, _ = seeded
    resp = _preview(client, "Tech/policy.md", page=1)
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "Tech/policy.md"
    assert data["dept"] == "Tech"
    assert data["kind"] == "md"
    assert data["page"] == 1
    assert [c["chunk_index"] for c in data["chunks"]] == [0, 1]
    assert data["chunks"][1]["text"] == LONG_TEXT
    assert data["truncated"] is False


def test_preview_without_a_location_returns_the_document(seeded):
    client, _ = seeded
    data = _preview(client, "Tech/policy.md").json()
    assert [c["chunk_index"] for c in data["chunks"]] == [0, 1, 2]


def test_preview_filters_by_page(seeded):
    client, _ = seeded
    data = _preview(client, "Tech/policy.md", page=2).json()
    assert [c["chunk_index"] for c in data["chunks"]] == [2]


def test_preview_keeps_departments_isolated(seeded):
    client, _ = seeded
    assert _preview(client, "Finance/budget.md", dept="Tech").status_code == 404
    assert _preview(client, "Finance/budget.md", dept="Finance").status_code == 200


def test_preview_accepts_several_departments(seeded):
    client, _ = seeded
    resp = client.get(
        "/sources/content",
        params=[
            ("source", "Finance/budget.md"),
            ("user_id", "u1"),
            ("user_depts", "Tech"),
            ("user_depts", "Finance"),
        ],
    )
    assert resp.status_code == 200


def test_preview_requires_a_valid_department(seeded):
    client, _ = seeded
    missing = _preview(client, "Tech/policy.md", dept=None)
    assert missing.status_code == 400
    assert missing.json()["detail"]["error"] == "missing_department"
    invalid = _preview(client, "Tech/policy.md", dept="Tech' or dept != '")
    assert invalid.status_code == 400


def test_preview_unknown_source_is_404(seeded):
    client, _ = seeded
    assert _preview(client, "Tech/missing.md").status_code == 404


def test_preview_respects_document_acl(seeded, tmp_path, monkeypatch):
    client, _ = seeded
    rules = tmp_path / "acl.json"
    rules.write_text(json.dumps({"Finance/budget.md": ["u_finance"]}))
    monkeypatch.setattr(settings, "ACL_ENABLED", True)
    monkeypatch.setattr(settings, "ACL_FILE", str(rules))
    acl.refresh()
    try:
        assert _preview(client, "Finance/budget.md", dept="Finance").status_code == 404
        allowed = client.get(
            "/sources/content",
            params={
                "source": "Finance/budget.md",
                "user_id": "u_finance",
                "user_dept": "Finance",
            },
        )
        assert allowed.status_code == 200
    finally:
        monkeypatch.setattr(settings, "ACL_ENABLED", False)
        acl.refresh()


def test_preview_is_gated_by_chat_api_key(seeded, monkeypatch):
    client, _ = seeded
    monkeypatch.setattr(settings, "CHAT_API_KEY", "k3y")
    try:
        assert _preview(client, "Tech/policy.md").status_code == 401
        ok = client.get(
            "/sources/content",
            params={"source": "Tech/policy.md", "user_dept": "Tech", "user_id": "u1"},
            headers={"X-API-Key": "k3y"},
        )
        assert ok.status_code == 200
    finally:
        monkeypatch.setattr(settings, "CHAT_API_KEY", "")


def test_preview_uses_the_token_in_jwt_mode(seeded, monkeypatch):
    from src import auth

    client, _ = seeded
    monkeypatch.setattr(settings, "AUTH_MODE", "jwt")
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", "test-secret-that-is-long-enough-123456")
    monkeypatch.setattr(settings, "AUTH_JWT_ALGORITHM", "HS256")
    try:
        token = auth.encode_token(
            auth.Identity(user_id="alice", departments=("Tech",))
        )
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get(
            "/sources/content", params={"source": "Tech/policy.md"}
        ).status_code == 401
        tech = client.get(
            "/sources/content",
            params=[
                ("source", "Tech/policy.md"),
                ("user_dept", "Finance"),
            ],
            headers=headers,
        )
        assert tech.status_code == 200
        assert tech.json()["dept"] == "Tech"
        finance = client.get(
            "/sources/content",
            params={"source": "Finance/budget.md"},
            headers=headers,
        )
        assert finance.status_code == 404
    finally:
        monkeypatch.setattr(settings, "AUTH_MODE", "none")


def test_file_endpoint_serves_the_original_after_access_check(seeded):
    client, _ = seeded
    resp = client.get(
        "/sources/file",
        params={"source": "Tech/policy.md", "user_id": "u1", "user_dept": "Tech"},
    )
    assert resp.status_code == 200
    assert LONG_TEXT in resp.text
    assert resp.headers["content-disposition"].startswith("inline")

    download = client.get(
        "/sources/file",
        params={
            "source": "Tech/policy.md",
            "user_id": "u1",
            "user_dept": "Tech",
            "download": 1,
        },
    )
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment")


def test_file_endpoint_keeps_departments_isolated(seeded):
    client, _ = seeded
    resp = client.get(
        "/sources/file",
        params={"source": "Finance/budget.md", "user_id": "u1", "user_dept": "Tech"},
    )
    assert resp.status_code == 404
    ok = client.get(
        "/sources/file",
        params={"source": "Finance/budget.md", "user_id": "u1", "user_dept": "Finance"},
    )
    assert ok.status_code == 200
    assert ok.text == "budget bytes"


def test_source_file_path_never_escapes_data_dir(seeded):
    from fastapi import HTTPException

    from src.sources_api import _source_file

    client, _ = seeded
    with pytest.raises(HTTPException) as excinfo:
        _source_file("../outside.txt")
    assert excinfo.value.status_code == 400
