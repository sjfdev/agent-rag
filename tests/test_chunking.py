"""Runtime chunking configuration and token-aware splitting.

运行时「分片」配置与 token 级切分测试。
"""

import json

import pytest

from src import chunk_settings, chunking
from src.config import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CHUNK_SETTINGS_FILE", str(tmp_path / "chunk_settings.json"))
    monkeypatch.setattr(settings, "CHUNK_TOKENS", 450)
    monkeypatch.setattr(settings, "CHUNK_OVERLAP_TOKENS", 50)
    monkeypatch.setattr(settings, "CHUNK_CHINESE_PUNCTUATION", True)
    monkeypatch.setattr(settings, "CHUNK_TITLE_PREFIX", True)
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "fake")
    chunk_settings.reset_state()
    chunking.reset_tokenizer()
    yield
    chunk_settings.reset_state()
    chunking.reset_tokenizer()


def _payload(**overrides):
    data = {
        "chunk_tokens": 420,
        "chunk_overlap_tokens": 40,
        "chinese_punctuation": True,
        "title_prefix": True,
    }
    data.update(overrides)
    return data


def test_env_is_the_default():
    described = chunk_settings.describe()
    assert described["source"] == "env"
    assert described["config"] == {
        "chunk_tokens": 450,
        "chunk_overlap_tokens": 50,
        "chinese_punctuation": True,
        "title_prefix": True,
    }
    assert described["signature"] == chunk_settings.signature()


def test_save_persists_atomically_and_describe_shows_override(tmp_path):
    saved = chunk_settings.save(_payload())
    assert saved["source"] == "override"
    assert saved["config"]["chunk_tokens"] == 420
    assert saved["config"]["chunk_overlap_tokens"] == 40
    path = tmp_path / "chunk_settings.json"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    on_disk = json.loads(path.read_text())
    assert on_disk["chunk_tokens"] == 420
    assert chunk_settings.effective()["chunk_tokens"] == 420


def test_clear_restores_env_and_removes_file(tmp_path):
    chunk_settings.save(_payload())
    cleared = chunk_settings.clear()
    assert cleared["source"] == "env"
    assert cleared["config"]["chunk_tokens"] == 450
    assert not (tmp_path / "chunk_settings.json").exists()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"chunk_tokens": 10}, "between 128 and 2048"),
        ({"chunk_tokens": 99999}, "between 128 and 2048"),
        ({"chunk_tokens": "many"}, "integer"),
        ({"chunk_overlap_tokens": -1}, "between 0"),
        ({"chunk_overlap_tokens": 500, "chunk_tokens": 450}, "smaller"),
        (
            {"chunk_overlap_tokens": 300, "chunk_tokens": 450},
            "half of chunk_tokens",
        ),
        ({"chinese_punctuation": "sometimes"}, "true or false"),
    ],
)
def test_validation_rejects_bad_input(overrides, message):
    with pytest.raises(ValueError, match=message):
        chunk_settings.save(_payload(**overrides))


def test_signature_changes_with_parameters_and_tokenizer(monkeypatch):
    base = chunk_settings.signature()
    assert chunk_settings.signature(_payload(chunk_tokens=420)) != base
    assert chunk_settings.signature(_payload(chinese_punctuation=False)) != base
    assert chunk_settings.signature(_payload(title_prefix=False)) != base
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "local")
    monkeypatch.setattr(settings, "LOCAL_EMBEDDING_MODEL", "other-model")
    assert chunk_settings.signature() != base
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "openai")
    assert chunk_settings.signature() != base
    monkeypatch.setattr(settings, "PDF_TABLES_ENABLED", not settings.PDF_TABLES_ENABLED)
    assert chunk_settings.signature() != base


def test_partial_override_file_merges_defaults(tmp_path):
    (tmp_path / "chunk_settings.json").write_text('{"chunk_tokens": 300}')
    chunk_settings.reset_state()
    effective = chunk_settings.effective()
    assert effective["chunk_tokens"] == 300
    assert effective["chunk_overlap_tokens"] == 50
    assert effective["chinese_punctuation"] is True


def test_title_prefix_sets_metadata_and_reserves_tokens(monkeypatch):
    monkeypatch.setattr(chunking, "count_tokens", len)
    config = {
        "chunk_tokens": 120,
        "chunk_overlap_tokens": 10,
        "chinese_punctuation": True,
        "title_prefix": True,
    }
    text = "这是一段用于测试标题前缀的中文内容。" * 20
    docs = chunking.split_section(text, {"source": "公共/Java.pdf", "page": 3}, config)
    assert len(docs) >= 2
    assert all(doc.page_content.startswith("Java\n") for doc in docs)
    assert all(doc.metadata["title"] == "Java" for doc in docs)
    assert all(doc.metadata["page"] == 3 for doc in docs)
    assert max(len(doc.page_content) for doc in docs) <= 120


def test_chinese_punctuation_keeps_sentence_endings(monkeypatch):
    monkeypatch.setattr(chunking, "count_tokens", len)
    config = {
        "chunk_tokens": 100,
        "chunk_overlap_tokens": 0,
        "chinese_punctuation": True,
        "title_prefix": False,
    }
    text = "第一句内容在这里。" * 20
    docs = chunking.split_section(text, {"source": "Tech/a.md"}, config)
    assert len(docs) >= 2
    assert all(doc.page_content.endswith("。") for doc in docs)


def test_document_hash_changes_when_chunking_changes():
    digest = "a" * 40
    first = chunking.document_hash(digest, _payload())
    same = chunking.document_hash(digest, _payload())
    changed = chunking.document_hash(digest, _payload(chunk_tokens=300))
    assert first == same
    assert changed != first
    assert len(first) == 40


def test_split_documents_preserves_section_metadata(monkeypatch):
    monkeypatch.setattr(chunking, "count_tokens", len)
    from langchain_core.documents import Document

    config = {
        "chunk_tokens": 80,
        "chunk_overlap_tokens": 0,
        "chinese_punctuation": True,
        "title_prefix": False,
    }
    docs = chunking.split_documents(
        [Document(page_content="内容。" * 40, metadata={"source": "Tech/b.md", "slide": 2})],
        config,
    )
    assert docs
    assert all(doc.metadata["source"] == "Tech/b.md" for doc in docs)
    assert all(doc.metadata["slide"] == 2 for doc in docs)
    assert all(doc.metadata["title"] == "b" for doc in docs)


def test_token_counts_are_cached():
    chunking.reset_tokenizer()
    first = chunking.count_tokens("这是一段需要重复计数的文本")
    second = chunking.count_tokens("这是一段需要重复计数的文本")
    assert first == second
    assert chunking._count_tokens_cached.cache_info().hits >= 1


def test_signature_follows_the_resolved_pdf_parser(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "PDF_PARSER", "pypdf")
    with_pypdf = chunk_settings.signature()
    monkeypatch.setattr(settings, "PDF_PARSER", "pdfium")
    assert chunk_settings.signature() != with_pypdf
