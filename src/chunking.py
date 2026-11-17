"""Token-aware chunking with optional Chinese punctuation and title prefixes.

The embedding model has a hard token window (512 for `bge-small-zh-v1.5`).
Splitting by characters made Chinese-heavy chunks exceed that window and get
silently truncated inside the embedding model. This module counts length with
the embedding tokenizer when it is available, falls back to a deterministic
character heuristic otherwise, and can prepend a document title to every chunk.

按 token 切分，可选中文标点优先与标题前缀。长度优先使用 embedding 的
tokenizer 计算；模型不可用时退回确定性的字符启发式，保证离线/测试环境可用。

The ETL folds :func:`chunk_settings.signature` into `doc_hash`, so changing a
chunking parameter makes the next incremental ingestion re-chunk existing
documents automatically. The chunking parameters themselves are stored in
`config/chunk_settings.json` and can be edited from the admin console.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src import chunk_settings
from src.config import settings

logger = logging.getLogger("agent-rag.chunking")

# RecursiveCharacterTextSplitter separators. Chinese punctuation is added when
# requested so a 450-token Chinese chunk is not cut in the middle of a sentence.
# 中文标点优先，避免在句子中间硬切；代码/英文仍按换行与空格优先。
SEPARATORS_DEFAULT = ["\n\n", "\n", " ", ""]
SEPARATORS_CHINESE = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]

_CJK = re.compile(r"[\u4e00-\u9fff]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

_tokenizer_lock = threading.Lock()
_tokenizer = None
_tokenizer_failed = False


def _load_tokenizer():
    """Lazy tokenizer for the local embedding model; None when unavailable."""
    global _tokenizer, _tokenizer_failed
    if _tokenizer is not None or _tokenizer_failed:
        return _tokenizer
    if str(settings.EMBEDDING_BACKEND or "").lower() != "local":
        _tokenizer_failed = True
        return None
    with _tokenizer_lock:
        if _tokenizer is None and not _tokenizer_failed:
            try:
                from transformers import AutoTokenizer

                _tokenizer = AutoTokenizer.from_pretrained(
                    settings.LOCAL_EMBEDDING_MODEL, local_files_only=True
                )
            except Exception as exc:  # not cached/offline: keep ingestion working
                _tokenizer_failed = True
                logger.warning(
                    "chunk tokenizer unavailable (%s); using character heuristic", exc
                )
    return _tokenizer


def reset_tokenizer() -> None:
    """Drop the cached tokenizer (tests and settings changes)."""
    global _tokenizer, _tokenizer_failed
    with _tokenizer_lock:
        _tokenizer = None
        _tokenizer_failed = False


def _heuristic_tokens(text: str) -> int:
    """Approximate token count for non-local embeddings / missing tokenizer.

    Chinese is counted per character (close to BERT tokenizers); remaining text
    is grouped as roughly four characters per token. Conservative enough for
    chunk boundaries, never used as the model's own encoding.
    """
    cjk = len(_CJK.findall(text))
    other = max(0, len(text) - cjk)
    return max(1, cjk + (other + 3) // 4)


def count_tokens(text: str) -> int:
    """Token length used by the splitter and the title-prefix budget."""
    tokenizer = _load_tokenizer()
    if tokenizer is not None:
        try:
            return len(tokenizer.encode(text, add_special_tokens=False))
        except Exception:
            return _heuristic_tokens(text)
    return _heuristic_tokens(text)


def load_config() -> Dict[str, Any]:
    """Effective configuration for one ingestion run."""
    return chunk_settings.effective()


def signature(config: Optional[Dict[str, Any]] = None) -> str:
    return chunk_settings.signature(config)


def document_hash(file_hash: str, config: Optional[Dict[str, Any]] = None) -> str:
    """File hash plus chunking signature; changing config changes this hash.

    The ETL stores this in the `doc_hash` metadata field. An incremental run
    therefore treats every previously indexed document as changed and re-chunks
    it, which is exactly what a chunking-parameter change requires.
    """
    body = f"{file_hash}|{chunk_settings.signature(config)}"
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def _clean_title(value: Any) -> str:
    title = _CONTROL.sub(" ", str(value or "")).strip()
    title = " ".join(title.split())
    return title[:120]


def _title_for(metadata: Dict[str, Any]) -> str:
    title = _clean_title(metadata.get("title"))
    if title:
        return title
    source = str(metadata.get("source") or "")
    if not source:
        return ""
    return _clean_title(Path(source).stem.replace("_", " "))


def _splitter(chunk_tokens: int, overlap: int, chinese_punctuation: bool):
    separators = SEPARATORS_CHINESE if chinese_punctuation else SEPARATORS_DEFAULT
    return RecursiveCharacterTextSplitter(
        chunk_size=max(1, int(chunk_tokens)),
        chunk_overlap=max(0, int(overlap)),
        separators=separators,
        length_function=count_tokens,
        keep_separator="end",
    )


def split_section(
    text: str,
    metadata: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[Document]:
    """Split one loader section (page/slide/sheet block) into token-sized chunks."""
    if not text or not text.strip():
        return []
    current = dict(config or load_config())
    meta = dict(metadata or {})
    title = _title_for(meta)
    # Keep the prefix comfortably inside the window even when the user picks
    # the minimum chunk size and the source filename is very long.
    title = title[: max(8, int(current["chunk_tokens"]) // 4)]
    prefix = f"{title}\n" if current.get("title_prefix") and title else ""
    prefix_tokens = count_tokens(prefix) if prefix else 0
    # Reserve the prefix inside the configured window so the final chunk still
    # fits the embedding model's token limit.
    limit = max(64, int(current["chunk_tokens"]) - prefix_tokens)
    overlap = min(int(current["chunk_overlap_tokens"]), max(0, limit // 2))
    splitter = _splitter(limit, overlap, bool(current.get("chinese_punctuation")))
    if title:
        meta["title"] = title
    chunks = []
    for body in splitter.split_text(text):
        body = body.strip()
        if not body:
            continue
        chunks.append(Document(page_content=prefix + body, metadata=dict(meta)))
    return chunks


def split_documents(
    documents: List[Document], config: Optional[Dict[str, Any]] = None
) -> List[Document]:
    """Split loader documents, preserving each section's metadata."""
    current = dict(config or load_config())
    out: List[Document] = []
    for document in documents:
        out.extend(
            split_section(
                document.page_content,
                document.metadata or {},
                current,
            )
        )
    return out


def summary(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compact description used in ingestion logs and the console."""
    current = dict(config or load_config())
    return {
        "chunk_tokens": int(current["chunk_tokens"]),
        "chunk_overlap_tokens": int(current["chunk_overlap_tokens"]),
        "chinese_punctuation": bool(current["chinese_punctuation"]),
        "title_prefix": bool(current["title_prefix"]),
        "signature": chunk_settings.signature(current),
        "tokenizer": chunk_settings.tokenizer_id(),
    }
