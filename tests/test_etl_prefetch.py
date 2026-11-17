"""Prefetch pipeline: same chunks as sequential mode, real parse/write overlap."""

import threading
import time
from pathlib import Path

import pytest

from src.config import settings
from src.engine import KnowledgeBaseEngine
from src.etl import EnterpriseETL, doc_id_for


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    db = tmp_path / "db"
    monkeypatch.setattr(settings, "DB_PATH", str(db))
    eng = KnowledgeBaseEngine()
    yield eng
    eng.reset()


@pytest.fixture()
def root(tmp_path):
    data = tmp_path / "data"
    (data / "Seq").mkdir(parents=True)
    (data / "Par").mkdir(parents=True)
    return data


def _write_files(folder: Path, count: int = 3) -> None:
    for i in range(count):
        (folder / f"doc{i}.md").write_text(
            f"# 文档 {i}\n\n第 {i} 份文件的正文，用于验证预取与串行结果一致。\n" * 12,
            encoding="utf-8",
        )


def test_prefetch_produces_the_same_chunks_as_sequential(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    _write_files(root / "Seq")
    _write_files(root / "Par")

    seq = EnterpriseETL(data_dir=str(root), prefetch=False)
    seq.process_department_folder("Seq", str(root / "Seq"))
    par = EnterpriseETL(data_dir=str(root), prefetch=True, prefetch_depth=2)
    par.process_department_folder("Par", str(root / "Par"))

    assert par.stats["added"] == seq.stats["added"] == 3
    assert par.stats["chunks"] == seq.stats["chunks"]
    assert par.stats["skipped"] == seq.stats["skipped"] == 0
    assert par.stats["failed"] == 0

    def bodies(dept: str):
        return sorted(d.page_content for d in engine.fetch_documents(dept))

    seq_bodies = bodies("Seq")
    assert bodies("Par") == seq_bodies

    # Same chunk counts per document, not just in total.
    summary = engine.doc_summary()
    seq_ids = [doc_id_for("Seq", f"Seq/doc{i}.md") for i in range(3)]
    par_ids = [doc_id_for("Par", f"Par/doc{i}.md") for i in range(3)]
    assert sorted(summary[d]["chunks"] for d in seq_ids) == sorted(
        summary[d]["chunks"] for d in par_ids
    )


def test_prefetch_parses_ahead_while_embedding_the_current_file(
    engine, root, monkeypatch
):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    _write_files(root / "Par")
    etl = EnterpriseETL(data_dir=str(root), prefetch=True, prefetch_depth=1)

    parse_calls = []
    consume_calls = []
    original_parse = EnterpriseETL._parse

    def timed_parse(self, plan):
        started = time.perf_counter()
        time.sleep(0.12)
        result = original_parse(self, plan)
        parse_calls.append((started, time.perf_counter(), threading.current_thread().name))
        return result

    def timed_consume(self, plan):
        started = time.perf_counter()
        time.sleep(0.06)
        consume_calls.append((started, time.perf_counter(), threading.current_thread().name))
        return plan.doc_id

    monkeypatch.setattr(EnterpriseETL, "_parse", timed_parse)
    monkeypatch.setattr(EnterpriseETL, "_consume", timed_consume)

    etl.process_department_folder("Par", str(root / "Par"))

    assert len(parse_calls) == 3
    assert len(consume_calls) == 3
    # Parsing happens on the worker, consumption on the calling thread.
    assert all(name == "ingest-prefetch" for _, _, name in parse_calls)
    assert all(name != "ingest-prefetch" for _, _, name in consume_calls)
    # The second document is parsed while the first one is being written.
    assert parse_calls[1][0] < consume_calls[0][1]


def test_prefetch_keeps_the_old_chunks_when_parsing_fails(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    path = root / "Par" / "doc0.md"
    path.write_text("# old\n\n旧内容，90 天轮换。", encoding="utf-8")
    first = EnterpriseETL(data_dir=str(root), prefetch=False)
    first.process_department_folder("Par", str(root / "Par"))
    assert first.stats["added"] == 1
    doc_id = doc_id_for("Par", "Par/doc0.md")

    # Change the file so it would be re-chunked, but make the parse fail.
    path.write_text("# new\n\n新内容，30 天轮换。", encoding="utf-8")
    from src.etl import load_file as real_load

    def broken(path_):
        if Path(path_).name == "doc0.md":
            raise ValueError("corrupt pdf")
        return real_load(path_)

    monkeypatch.setattr("src.etl.load_file", broken)
    etl = EnterpriseETL(data_dir=str(root), prefetch=True)
    etl.process_department_folder("Par", str(root / "Par"))

    assert etl.stats["failed"] == 1
    assert etl.stats["updated"] == 0
    bodies = [d.page_content for d in engine.fetch_documents("Par")]
    assert any("旧内容" in body for body in bodies)
    assert not any("新内容" in body for body in bodies)
    assert engine.doc_summary()[doc_id]["chunks"] >= 1


def test_prefetch_worker_crash_falls_back_to_inline_parsing(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    _write_files(root / "Par", count=2)
    etl = EnterpriseETL(data_dir=str(root), prefetch=True, prefetch_depth=1)

    original = EnterpriseETL._parse
    state = {"calls": 0}

    def flaky(self, plan):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("worker exploded")
        return original(self, plan)

    monkeypatch.setattr(EnterpriseETL, "_parse", flaky)
    etl.process_department_folder("Par", str(root / "Par"))

    assert etl.stats["added"] == 2
    assert etl.stats["failed"] == 0


def test_prefetch_skips_unchanged_files_without_parsing(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    _write_files(root / "Par")
    EnterpriseETL(data_dir=str(root), prefetch=False).process_department_folder(
        "Par", str(root / "Par")
    )

    parsed = []
    original = EnterpriseETL._parse

    def counting(self, plan):
        parsed.append(plan.source)
        return original(self, plan)

    monkeypatch.setattr(EnterpriseETL, "_parse", counting)
    etl = EnterpriseETL(data_dir=str(root), prefetch=True)
    etl.process_department_folder("Par", str(root / "Par"))

    assert etl.stats["skipped"] == 3
    assert parsed == []


def test_prefetch_depth_is_clamped_and_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "INGEST_PREFETCH", False)
    monkeypatch.setattr(settings, "INGEST_PREFETCH_DEPTH", 1)
    etl = EnterpriseETL()
    assert etl.prefetch is False
    assert etl.prefetch_depth == 1
    assert EnterpriseETL(prefetch_depth=99).prefetch_depth == 8
    assert EnterpriseETL(prefetch_depth=0).prefetch_depth == 1
    assert EnterpriseETL(prefetch=True).prefetch is True


def test_pipeline_description_reflects_mode(monkeypatch):
    monkeypatch.setattr(settings, "INGEST_PREFETCH", False)
    assert "sequential" in EnterpriseETL().pipeline_description()
    assert "prefetch depth=2" in EnterpriseETL(prefetch=True, prefetch_depth=2).pipeline_description()


def test_prefetch_thread_target_leaves_headroom_for_the_parser():
    from src.etl import prefetch_thread_target

    assert prefetch_thread_target(7) == 5
    assert prefetch_thread_target(4) == 2
    assert prefetch_thread_target(2) == 1
    assert prefetch_thread_target(1) == 1


def test_pipeline_caps_and_restores_embedding_threads(engine, root, monkeypatch):
    monkeypatch.setattr("src.etl.kb_engine", engine)
    _write_files(root / "Par", count=2)
    etl = EnterpriseETL(data_dir=str(root), prefetch=True, prefetch_depth=1)
    calls = []
    monkeypatch.setattr(EnterpriseETL, "_cap_embedding_threads", lambda self: 3)
    monkeypatch.setattr(
        EnterpriseETL, "_restore_embedding_threads", lambda self, prev: calls.append(prev)
    )
    etl.process_department_folder("Par", str(root / "Par"))
    assert calls == [3]
    assert etl.stats["added"] == 2
