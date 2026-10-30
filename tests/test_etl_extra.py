"""ETL edge cases: skipped files, oversized input, empty text, logging hook."""


import pytest

from src.config import settings
from src.engine import KnowledgeBaseEngine
from src.etl import EnterpriseETL, doc_id_for


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "db"))
    eng = KnowledgeBaseEngine()
    yield eng
    eng.reset()


@pytest.fixture()
def root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "Tech").mkdir(parents=True)
    monkeypatch.setattr(settings, "DATA_DIR", str(data))
    return data


def test_empty_file_is_skipped(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    (root / "Tech" / "empty.md").write_text("")
    logs = []
    etl = EnterpriseETL(data_dir=str(root), on_log=logs.append)
    etl.process_department_folder("Tech", str(root / "Tech"))
    assert etl.stats["failed"] == 1
    assert any("empty file" in line for line in logs)


def test_oversized_file_is_skipped(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    monkeypatch.setattr("src.etl.MAX_FILE_BYTES", 10)
    (root / "Tech" / "big.md").write_text("x" * 100)
    etl = EnterpriseETL(data_dir=str(root))
    etl.process_department_folder("Tech", str(root / "Tech"))
    assert etl.stats["failed"] == 1


def test_no_extractable_text_is_skipped(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    monkeypatch.setattr("src.etl.load_file", lambda path: [])
    (root / "Tech" / "blank.md").write_text("# nothing")
    etl = EnterpriseETL(data_dir=str(root))
    etl.process_department_folder("Tech", str(root / "Tech"))
    assert etl.stats["failed"] == 1


def test_legacy_and_hidden_files_are_ignored(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    (root / "Tech" / "old.doc").write_bytes(b"legacy")
    (root / "Tech" / ".hidden.md").write_text("hidden")
    (root / "Tech" / "~$lock.md").write_text("lock")
    (root / "Tech" / "ok.md").write_text("# ok\n\ncontent")
    logs = []
    etl = EnterpriseETL(data_dir=str(root), on_log=logs.append)
    seen = etl.process_department_folder("Tech", str(root / "Tech"))
    assert len(seen) == 1
    assert any("legacy .doc" in line for line in logs)


def test_department_without_files(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    empty = root / "Empty"
    empty.mkdir()
    etl = EnterpriseETL(data_dir=str(root))
    assert etl.process_department_folder("Empty", str(empty)) == set()


def test_logging_callback_failure_is_ignored(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)

    def boom(_message):
        raise RuntimeError("logger broken")

    (root / "Tech" / "ok.md").write_text("# ok\n\ncontent")
    etl = EnterpriseETL(data_dir=str(root), on_log=boom)
    etl.process_department_folder("Tech", str(root / "Tech"))
    assert etl.stats["added"] == 1


def test_prune_is_skipped_during_rebuild(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    engine.add_documents(
        ["x"],
        [{"dept": "Tech", "source": "Tech/gone.md", "doc_id": "gone", "doc_hash": "h",
          "kind": "md", "page": 1, "slide": 0, "sheet": "", "title": "", "chunk_index": 0}],
    )
    engine.begin_rebuild()
    try:
        etl = EnterpriseETL(data_dir=str(root))
        assert etl.prune(set()) == 0
    finally:
        engine.abort_rebuild()


def test_doc_id_is_stable_and_dept_scoped():
    a = doc_id_for("Tech", "Tech/a.md")
    assert a == doc_id_for("Tech", "Tech/a.md")
    assert a != doc_id_for("HR", "Tech/a.md")


def test_source_name_falls_back_outside_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    etl = EnterpriseETL(data_dir=str(tmp_path / "data"))
    outside = tmp_path / "other.md"
    outside.write_text("x")
    assert etl._source_name(outside) == "other.md"
