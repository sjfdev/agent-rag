"""Document-level ACL and multi-department retrieval."""

import json

import pytest

from src import acl
from src.config import settings
from src.engine import KnowledgeBaseEngine, dept_filter


@pytest.fixture()
def acl_file(tmp_path, monkeypatch):
    path = tmp_path / "acl.json"
    monkeypatch.setattr(settings, "ACL_ENABLED", True)
    monkeypatch.setattr(settings, "ACL_FILE", str(path))
    acl.refresh()
    yield path
    acl.refresh()


def _write(path, rules):
    path.write_text(json.dumps(rules))


def test_no_rule_means_public(acl_file):
    _write(acl_file, {"Finance/budget.pdf": ["u_finance"]})
    assert acl.is_allowed("Tech/policy.md", "u1")
    assert not acl.is_allowed("Finance/budget.pdf", "u1")
    assert acl.is_allowed("Finance/budget.pdf", "u_finance")


def test_group_membership(acl_file):
    _write(acl_file, {"HR/salaries.xlsx": ["group:hr"]})
    assert acl.is_allowed("HR/salaries.xlsx", "u1", ["hr"])
    assert acl.is_allowed("HR/salaries.xlsx", "u1", ["group:hr"])
    assert not acl.is_allowed("HR/salaries.xlsx", "u1", ["finance"])
    assert not acl.is_allowed("HR/salaries.xlsx", "u1")


def test_bare_path_rule_matches(acl_file):
    _write(acl_file, {"budget.pdf": ["u_finance"]})
    assert acl.is_allowed("Finance/budget.pdf", "u_finance")
    assert not acl.is_allowed("Finance/budget.pdf", "u1")


def test_disabled_acl_allows_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ACL_ENABLED", False)
    assert acl.is_allowed("Finance/budget.pdf", "u1")


def test_missing_file_allows_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ACL_ENABLED", True)
    monkeypatch.setattr(settings, "ACL_FILE", str(tmp_path / "missing.json"))
    acl.refresh()
    assert acl.is_allowed("Finance/budget.pdf", "u1")


def test_filter_documents(acl_file):
    from langchain_core.documents import Document

    _write(acl_file, {"Finance/budget.pdf": ["u_finance"]})
    docs = [
        Document(page_content="a", metadata={"source": "Tech/a.md"}),
        Document(page_content="b", metadata={"source": "Finance/budget.pdf"}),
    ]
    kept = acl.filter_documents(docs, "u1")
    assert [d.metadata["source"] for d in kept] == ["Tech/a.md"]
    kept = acl.filter_documents(docs, "u_finance")
    assert len(kept) == 2


def test_rule_file_reloads_on_change(acl_file):
    _write(acl_file, {"Tech/a.md": ["u1"]})
    assert not acl.is_allowed("Tech/a.md", "u2")
    import os
    stat = acl_file.stat()
    _write(acl_file, {"Tech/a.md": ["u2"]})
    os.utime(acl_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert acl.is_allowed("Tech/a.md", "u2")


def test_dept_filter_expression():
    assert dept_filter("Tech") == "dept == 'Tech'"
    assert dept_filter(["Tech"]) == "dept == 'Tech'"
    assert dept_filter(["Tech", "HR"]) == "dept in ['Tech', 'HR']"
    with pytest.raises(ValueError):
        dept_filter(["bad dept"])


def test_multi_department_search(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    engine = KnowledgeBaseEngine()

    def meta(dept, source, doc_id):
        return {
            "dept": dept, "source": source, "doc_id": doc_id, "doc_hash": "h",
            "kind": "md", "page": 1, "slide": 0, "sheet": "", "title": "",
            "chunk_index": 0,
        }

    engine.add_documents(
        ["one approving reviewer", "confidential salary bands", "visitor wifi"],
        [
            meta("Tech", "Tech/a.md", "t1"),
            meta("Finance", "Finance/b.md", "f1"),
            meta("Demo", "Demo/c.md", "d1"),
        ],
    )
    tech_only = engine.search("salary bands", ["Tech"], 5)
    assert {d.metadata["dept"] for d in tech_only} == {"Tech"}

    tech_finance = engine.search("salary bands", ["Tech", "Finance"], 5)
    assert {d.metadata["dept"] for d in tech_finance} <= {"Tech", "Finance"}
    assert "Finance" in {d.metadata["dept"] for d in tech_finance}
    assert "Demo" not in {d.metadata["dept"] for d in tech_finance}
