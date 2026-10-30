"""ETL incrementality and safe replacement against a real FAISS store."""

from pathlib import Path

import pytest

from src.engine import KnowledgeBaseEngine
from src.etl import EnterpriseETL, doc_id_for


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    from src.config import settings

    db = tmp_path / "db"
    monkeypatch.setattr(settings, "DB_PATH", str(db))
    eng = KnowledgeBaseEngine()
    yield eng
    import shutil

    shutil.rmtree(db, ignore_errors=True)


@pytest.fixture()
def data_dir(tmp_path):
    root = tmp_path / "data"
    (root / "Tech").mkdir(parents=True)
    return root


def _ingest(engine, root: Path, dept="Tech"):
    etl = EnterpriseETL(data_dir=str(root))
    etl.process_department_folder(dept, str(root / dept))
    return etl


def test_first_ingestion_adds_chunks(engine, data_dir, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    (data_dir / "Tech" / "policy.md").write_text("# Policy\n\nRotate every 90 days.")
    etl = _ingest(engine, data_dir)
    assert etl.stats["added"] == 1
    assert etl.stats["chunks"] >= 1
    assert engine.live_count() == etl.stats["chunks"]


def test_second_ingestion_skips_unchanged(engine, data_dir, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    (data_dir / "Tech" / "policy.md").write_text("# Policy\n\nRotate every 90 days.")
    _ingest(engine, data_dir)
    before = engine.live_count()
    etl = _ingest(engine, data_dir)
    assert etl.stats["skipped"] == 1
    assert etl.stats["added"] == 0
    assert engine.live_count() == before


def test_changed_file_is_replaced_without_duplicates(engine, data_dir, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    path = data_dir / "Tech" / "policy.md"
    path.write_text("# Policy\n\nRotate every 90 days.")
    first = _ingest(engine, data_dir)
    before_chunks = engine.live_count()

    path.write_text("# Policy v2\n\nRotate every 30 days and log it.")
    etl = _ingest(engine, data_dir)
    assert etl.stats["updated"] == 1
    assert engine.live_count() == before_chunks

    doc_id = doc_id_for("Tech", "Tech/policy.md")
    summary = engine.doc_summary()
    assert summary[doc_id]["chunks"] == first.stats["chunks"]
    bodies = [d.page_content for d in engine.fetch_documents("Tech")]
    assert any("30 days" in body for body in bodies)
    assert not any("90 days" in body for body in bodies)


def test_prune_removes_vanished_files(engine, data_dir, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    await_path = data_dir / "Tech" / "keep.md"
    gone_path = data_dir / "Tech" / "gone.md"
    await_path.write_text("# Keep\n\nstill here")
    gone_path.write_text("# Gone\n\nremoved soon")
    etl = _ingest(engine, data_dir)
    assert engine.live_count() == etl.stats["chunks"]

    gone_path.unlink()
    etl = _ingest(engine, data_dir)
    pruned = etl.prune({doc_id_for("Tech", "Tech/keep.md")})
    assert pruned == 1
    remaining = engine.fetch_documents("Tech")
    sources = {d.metadata["source"] for d in remaining}
    assert "Tech/gone.md" not in sources


def test_parse_failure_keeps_previous_copy(engine, data_dir, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    path = data_dir / "Tech" / "policy.md"
    path.write_text("# Policy\n\nRotate every 90 days.")
    _ingest(engine, data_dir)
    before = engine.live_count()

    def broken(_path):
        raise ValueError("cannot parse")

    monkeypatch.setattr("src.etl.load_file", broken)
    path.write_text("# Policy v2\n\nbroken")
    etl = _ingest(engine, data_dir)
    assert etl.stats["failed"] == 1
    assert engine.live_count() == before
    bodies = [d.page_content for d in engine.fetch_documents("Tech")]
    assert any("90 days" in body for body in bodies)
