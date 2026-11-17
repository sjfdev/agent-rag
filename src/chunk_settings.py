"""Runtime chunking overrides set from the admin console.

管理台运行时「分片」配置。

`.env` stays the default (`CHUNK_TOKENS` / `CHUNK_OVERLAP_TOKENS` /
`CHUNK_CHINESE_PUNCTUATION` / `CHUNK_TITLE_PREFIX`). The console can change the
token window, overlap, punctuation-aware splitting and title prefixing without
a restart: overrides persist to `CHUNK_SETTINGS_FILE` (default
`./config/chunk_settings.json`, mode 0600, gitignored) and the next ingestion
reads them through :func:`effective`.

`.env` 仍是默认值；管理台修改分片窗口、重叠、中文标点优先与标题前缀后，
覆盖项持久化到 `config/chunk_settings.json`（0600、gitignore），下一次入库
通过 :func:`effective` 读取，无需重启。

Every effective configuration has a stable :func:`signature`. The ETL folds that
signature into `doc_hash`, so changing a chunking parameter makes the next
incremental ingestion re-chunk already indexed documents automatically instead
of silently skipping them (the file content hash did not change).

每套生效配置都有稳定的 :func:`signature`。ETL 把签名并入 `doc_hash`，因此
修改分片参数后，下一次增量入库会把已入库文档自动重切，而不是因为文件内容
没变而跳过。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict

from src.config import settings

logger = logging.getLogger("agent-rag.chunking")

# Bump when the splitting algorithm itself changes (not when a parameter does).
# 切分算法本身变化时递增（参数变化已由签名覆盖）。
CHUNKER_VERSION = 1

MIN_TOKENS = 128
MAX_TOKENS = 2048
MAX_OVERLAP = 1024

_lock = threading.Lock()
_state: Dict[str, Any] = {"path": None, "mtime": None, "data": {}, "generation": 0}


def _path() -> Path:
    return Path(settings.CHUNK_SETTINGS_FILE).expanduser()


def tokenizer_id() -> str:
    """Identity of the tokenizer used for token counting.

    The local sentence-transformers model is counted with its own tokenizer.
    Other embedding backends fall back to a deterministic character heuristic,
    which is reflected in the signature so a later model switch re-chunks.
    """
    backend = str(settings.EMBEDDING_BACKEND or "").lower()
    if backend == "local":
        return f"local:{settings.LOCAL_EMBEDDING_MODEL}"
    if backend == "openai":
        return f"openai:{settings.EMBEDDING_MODEL}"
    return f"heuristic:{backend or 'unknown'}"


def env_defaults() -> Dict[str, Any]:
    """Defaults from `.env` (`CHUNK_*`). / 从 `.env` 读取分片默认值。"""
    return {
        "chunk_tokens": int(settings.CHUNK_TOKENS),
        "chunk_overlap_tokens": int(settings.CHUNK_OVERLAP_TOKENS),
        "chinese_punctuation": bool(settings.CHUNK_CHINESE_PUNCTUATION),
        "title_prefix": bool(settings.CHUNK_TITLE_PREFIX),
    }


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
        return value.strip().lower() in {"true", "1"}
    raise ValueError(f"{name} must be true or false")


def _validate(payload: Dict[str, Any], base: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return a complete, normalised configuration; raise ValueError on bad input."""
    defaults = dict(base or env_defaults())
    try:
        tokens = int(payload.get("chunk_tokens", defaults["chunk_tokens"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("chunk_tokens must be an integer") from exc
    if not MIN_TOKENS <= tokens <= MAX_TOKENS:
        raise ValueError(f"chunk_tokens must be between {MIN_TOKENS} and {MAX_TOKENS}")

    try:
        overlap = int(payload.get("chunk_overlap_tokens", defaults["chunk_overlap_tokens"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("chunk_overlap_tokens must be an integer") from exc
    if not 0 <= overlap <= MAX_OVERLAP:
        raise ValueError(f"chunk_overlap_tokens must be between 0 and {MAX_OVERLAP}")
    if overlap >= tokens:
        raise ValueError("chunk_overlap_tokens must be smaller than chunk_tokens")
    if overlap > tokens // 2:
        raise ValueError("chunk_overlap_tokens must not exceed half of chunk_tokens")

    return {
        "chunk_tokens": tokens,
        "chunk_overlap_tokens": overlap,
        "chinese_punctuation": _as_bool(
            payload.get("chinese_punctuation", defaults["chinese_punctuation"]),
            "chinese_punctuation",
        ),
        "title_prefix": _as_bool(
            payload.get("title_prefix", defaults["title_prefix"]), "title_prefix"
        ),
    }


def _read() -> Dict[str, Any]:
    """Overrides on disk, re-read when the file changes. Corrupt files are ignored."""
    path = _path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    with _lock:
        if _state["path"] == str(path) and _state["mtime"] == mtime:
            return dict(_state["data"])
        data: Dict[str, Any] = {}
        if mtime is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                data = _validate(raw)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("ignoring %s: %s", path, exc)
                data = {}
        _state.update(
            {
                "path": str(path),
                "mtime": mtime,
                "data": data,
                "generation": _state["generation"] + 1,
            }
        )
        return dict(data)


def reset_state() -> None:
    """Drop the in-process cache (tests and admin tooling only)."""
    with _lock:
        _state.update({"path": None, "mtime": None, "data": {}, "generation": 0})


def generation() -> int:
    _read()
    with _lock:
        return int(_state["generation"])


def overrides() -> Dict[str, Any]:
    return _read()


def effective() -> Dict[str, Any]:
    """`.env` defaults merged with on-disk overrides. / 合并默认值与磁盘覆盖项。"""
    config = env_defaults()
    config.update(_read())
    return config


def _pdf_parser_id() -> str:
    """Resolved PDF extractor name for the chunk signature (no hard import)."""
    try:
        from src.loaders import pdf_parser_name

        return pdf_parser_name()
    except Exception:
        return "unknown"


def signature(config: Dict[str, Any] | None = None) -> str:
    """Stable fingerprint of the effective chunking configuration.

    Includes the tokenizer identity and algorithm version, so switching the
    embedding model or upgrading the splitter also triggers re-chunking.
    """
    current = dict(config or effective())
    payload = {
        "v": CHUNKER_VERSION,
        "tokenizer": tokenizer_id(),
        "chunk_tokens": int(current["chunk_tokens"]),
        "chunk_overlap_tokens": int(current["chunk_overlap_tokens"]),
        "chinese_punctuation": bool(current["chinese_punctuation"]),
        "title_prefix": bool(current["title_prefix"]),
        # Parser switches also change the extracted text even when the file
        # bytes are unchanged; folding them in makes the next ingestion
        # re-index automatically (instead of silently skipping).
        "parser": {
            "xlsx_rows": int(settings.XLSX_ROWS_PER_CHUNK),
            "csv_rows": int(settings.CSV_ROWS_PER_CHUNK),
            "xlsx_max_rows": int(settings.XLSX_MAX_ROWS),
            "pdf_tables": bool(settings.PDF_TABLES_ENABLED),
            # The extractor changes the text itself, and "auto" resolves
            # differently depending on whether pypdfium2 is installed.
            "pdf_parser": _pdf_parser_id(),
            "ocr": bool(settings.OCR_ENABLED),
            "ocr_min_chars": int(settings.OCR_MIN_CHARS),
            "ocr_lang": str(settings.OCR_LANG),
        },
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def describe() -> Dict[str, Any]:
    """Console view: effective values, env defaults, signature and source."""
    config = effective()
    override = _read()
    return {
        "source": "override" if override else "env",
        "config": config,
        "env": env_defaults(),
        "signature": signature(config),
        "tokenizer": tokenizer_id(),
        "path": str(_path()),
    }


def save(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist overrides atomically (0600). / 原子写入覆盖配置（0600）。"""
    merged = _validate(payload, base=effective())
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = json.dumps(
        {"version": 1, **merged}, ensure_ascii=False, indent=2, sort_keys=True
    )
    with _lock:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        _state.update(
            {
                "path": str(path),
                "mtime": path.stat().st_mtime_ns,
                "data": dict(merged),
                "generation": _state["generation"] + 1,
            }
        )
    logger.info("chunk settings saved: %s", merged)
    return describe()


def clear() -> Dict[str, Any]:
    """Remove overrides and fall back to `.env`. / 删除覆盖项，回退 `.env`。"""
    path = _path()
    with _lock:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        _state.update(
            {"path": None, "mtime": None, "data": {}, "generation": _state["generation"] + 1}
        )
    return describe()
