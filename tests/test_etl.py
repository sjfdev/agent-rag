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


def test_chunking_change_rechunks_an_unchanged_file(engine, data_dir, monkeypatch, tmp_path):
    """A chunking change must invalidate the file-hash skip and re-chunk.

    修改分片参数后，即使文件内容没变，下一次增量入库也要重切。
    """
    from src import chunk_settings, chunking
    from src.config import settings
    from src.hashing import file_sha1

    monkeypatch.setattr("src.etl.kb_engine", engine)
    monkeypatch.setattr(settings, "CHUNK_SETTINGS_FILE", str(tmp_path / "chunk_settings.json"))
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "fake")
    monkeypatch.setattr(chunking, "count_tokens", len)
    chunk_settings.reset_state()
    chunking.reset_tokenizer()

    path = data_dir / "Tech" / "policy.md"
    path.write_text("这是一段用于测试分片配置变化的句子。" * 20)

    chunk_settings.save(
        {
            "chunk_tokens": 150,
            "chunk_overlap_tokens": 20,
            "chinese_punctuation": True,
            "title_prefix": True,
        }
    )
    first = _ingest(engine, data_dir)
    assert first.stats["added"] == 1
    assert first.stats["chunks"] >= 2
    first_chunks = engine.live_count()

    same = _ingest(engine, data_dir)
    assert same.stats["skipped"] == 1
    assert engine.live_count() == first_chunks

    # Change only the chunk window: the file bytes are identical, but the
    # signature folded into doc_hash must force an update.
    chunk_settings.save(
        {
            "chunk_tokens": 240,
            "chunk_overlap_tokens": 20,
            "chinese_punctuation": True,
            "title_prefix": True,
        }
    )
    changed = _ingest(engine, data_dir)
    assert changed.stats["updated"] == 1
    assert changed.stats["skipped"] == 0
    assert engine.live_count() == changed.stats["chunks"]

    bodies = [doc.page_content for doc in engine.fetch_documents("Tech")]
    assert any(body.startswith("policy\n") for body in bodies)

    doc_id = doc_id_for("Tech", "Tech/policy.md")
    expected = chunking.document_hash(file_sha1(path), chunk_settings.effective())
    assert engine.doc_summary()[doc_id]["doc_hash"] == expected
    assert engine.doc_summary()[doc_id]["doc_hash"] != file_sha1(path)
