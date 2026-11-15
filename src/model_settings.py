"""Runtime LLM overrides set from the admin console.

管理台运行时 LLM 覆盖配置。

`.env` stays the default (`LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` /
`LLM_TEMPERATURE` / `LLM_MAX_TOKENS`). The console can point chat generation at
a different endpoint or model without a restart: overrides are persisted to
`MODEL_SETTINGS_FILE` (default `./config/model_settings.json`, mode 0600 and
gitignored because it may hold an API key) and picked up by `src/agent.py` on
the next turn.

`.env` 仍是默认值（`LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` /
`LLM_TEMPERATURE` / `LLM_MAX_TOKENS`）。管理台可以不改重启地把对话生成指向
其他端点或模型：覆盖项持久化到 `MODEL_SETTINGS_FILE`（默认
`./config/model_settings.json`，权限 0600，因可能含 API Key 而 gitignore），
由 `src/agent.py` 在下一轮对话读取。

Only chat generation is affected. Ingestion and retrieval keep using the
`EMBEDDING_*` settings, and `LLM_TIMEOUT` / `LLM_MAX_RETRIES` remain
environment-only knobs. Deleting the file (or `DELETE /admin/api/model`) falls
back to `.env`.

只影响对话生成。入库与检索仍使用 `EMBEDDING_*` 设置，
`LLM_TIMEOUT` / `LLM_MAX_RETRIES` 仍只从环境变量读取。删除该文件
（或调用 `DELETE /admin/api/model`）即回退到 `.env`。

The API key never leaves the process: `describe()` returns a masked hint, and
saving with an empty key keeps the current one.

API Key 不会明文离开进程：`describe()` 只返回掩码提示；保存时留空则保留原 Key。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import settings

logger = logging.getLogger("agent-rag.model")

FIELDS = ("api_base", "api_key", "model", "temperature", "max_tokens")
_MAX_BASE = 300
_MAX_MODEL = 200
_MAX_KEY = 512

_lock = threading.Lock()
_state: Dict[str, Any] = {"path": None, "mtime": None, "data": {}, "generation": 0}


def _path() -> Path:
    return Path(settings.MODEL_SETTINGS_FILE).expanduser()


def env_defaults() -> Dict[str, Any]:
    """Defaults from `.env` (`LLM_*`).

    从 `.env` 的 `LLM_*` 读取默认配置。
    """
    return {
        "api_base": str(settings.LLM_API_BASE).rstrip("/"),
        "api_key": settings.LLM_API_KEY,
        "model": settings.LLM_MODEL,
        "temperature": float(settings.LLM_TEMPERATURE),
        "max_tokens": int(settings.LLM_MAX_TOKENS),
    }


def _validate(payload: Dict[str, Any], *, require_key: bool = False) -> Dict[str, Any]:
    """Return the normalised subset of `payload`; raise ValueError on bad input.

    返回 `payload` 规范化后的字段子集；输入非法时抛出 ValueError。
    """
    base = str(payload.get("api_base") or "").strip()
    if not base:
        raise ValueError("api_base is required")
    if len(base) > _MAX_BASE or any(ch.isspace() for ch in base):
        raise ValueError("api_base must be a single URL without spaces")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise ValueError("api_base must start with http:// or https://")

    model = str(payload.get("model") or "").strip()
    if not model:
        raise ValueError("model is required")
    if len(model) > _MAX_MODEL or any(ord(ch) < 32 for ch in model):
        raise ValueError("model name is too long or contains control characters")

    try:
        temperature = float(payload.get("temperature", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("temperature must be a number") from exc
    if not 0.0 <= temperature <= 2.0:
        raise ValueError("temperature must be between 0 and 2")

    try:
        max_tokens = int(payload.get("max_tokens", 256))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_tokens must be an integer") from exc
    if not 1 <= max_tokens <= 32768:
        raise ValueError("max_tokens must be between 1 and 32768")

    cleaned: Dict[str, Any] = {
        "api_base": base.rstrip("/"),
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    key = payload.get("api_key")
    if key is not None and str(key).strip():
        key = str(key).strip()
        if len(key) > _MAX_KEY:
            raise ValueError("api_key is too long")
        cleaned["api_key"] = key
    elif require_key:
        raise ValueError("api_key is required")
    return cleaned


def _read() -> Dict[str, Any]:
    """Overrides on disk, re-read when the file changes. Corrupt files are ignored.

    读取磁盘上的覆盖配置；文件变化时重新读取，损坏文件直接忽略。
    """
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
            {"path": str(path), "mtime": mtime, "data": data, "generation": _state["generation"] + 1}
        )
        return dict(data)


def reset_state() -> None:
    """Drop the in-process cache (tests and admin tooling only).

    清除进程内缓存（仅测试与管理工具使用）。
    """
    with _lock:
        _state.update({"path": None, "mtime": None, "data": {}, "generation": 0})


def generation() -> int:
    """Bumped whenever the effective configuration changes (cache key).

    生效配置每次变化时自增（用作缓存键）。
    """
    _read()
    with _lock:
        return int(_state["generation"])


def cache_key():
    """Identity of the current configuration (path + generation).

    当前配置的身份标识（路径 + generation）。
    """
    _read()
    with _lock:
        return str(_state["path"]), int(_state["generation"])


def overrides() -> Dict[str, Any]:
    return _read()


def effective() -> Dict[str, Any]:
    """`.env` defaults merged with on-disk overrides.

    合并 `.env` 默认值与磁盘覆盖项。
    """
    config = env_defaults()
    config.update(_read())
    return config


def describe() -> Dict[str, Any]:
    """What the console may see: no raw secret, plus where each value came from.

    管理台可见的视图：不含明文密钥，并标明取值来源（env / override）。
    """
    config = effective()
    overrides_on_disk = _read()
    key = config.get("api_key") or ""
    hint = f"****{key[-4:]}" if len(key) > 8 else ("****" if key else "")
    # Never return the raw secret: only a "****1234" style hint.
    # 绝不返回明文密钥，只返回 "****1234" 形式的提示。
    return {
        "source": "override" if overrides_on_disk else "env",
        "api_base": config["api_base"],
        "model": config["model"],
        "temperature": config["temperature"],
        "max_tokens": config["max_tokens"],
        "has_api_key": bool(key),
        "api_key_hint": hint,
        "env": {
            "api_base": str(settings.LLM_API_BASE).rstrip("/"),
            "model": settings.LLM_MODEL,
            "temperature": float(settings.LLM_TEMPERATURE),
            "max_tokens": int(settings.LLM_MAX_TOKENS),
        },
        "path": str(_path()),
    }


def save(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist overrides. A blank api_key keeps the currently effective one.

    持久化覆盖配置；api_key 留空时保留当前生效的 Key。
    """
    cleaned = _validate(payload)
    if "api_key" not in cleaned:
        cleaned["api_key"] = effective().get("api_key", "")

    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a temp file, chmod/lock it down, then atomically replace the target.
    # 先写临时文件、收紧权限，再原子替换目标文件，避免读到半截内容。
    tmp = path.with_name(path.name + ".tmp")
    body = json.dumps({"version": 1, **cleaned}, ensure_ascii=False, indent=2, sort_keys=True)
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
                "data": dict(cleaned),
                "generation": _state["generation"] + 1,
            }
        )
    logger.info("model overrides saved (model=%s, base=%s)", cleaned["model"], cleaned["api_base"])
    return describe()


def clear() -> Dict[str, Any]:
    """Remove the overrides file and fall back to `.env`.

    删除覆盖文件并回退到 `.env`。
    """
    path = _path()
    with _lock:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        _state.update({"path": None, "mtime": None, "data": {}, "generation": _state["generation"] + 1})
    return describe()


def probe(candidate: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> Dict[str, Any]:
    """Send a minimal completion so the console can test before/after saving.

    发送一次最小补全请求，供管理台在保存前后测试连接。
    """
    merged = dict(effective())
    if candidate:
        merged.update({k: v for k, v in candidate.items() if v is not None})
        if not str(candidate.get("api_key") or "").strip():
            merged["api_key"] = effective().get("api_key", "")
    checked = _validate(merged, require_key=False)
    checked.setdefault("api_key", merged.get("api_key", ""))

    started = time.monotonic()
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=checked["api_base"], api_key=checked["api_key"] or "none", timeout=timeout
        )
        client.chat.completions.create(
            model=checked["model"],
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=8,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the console, not raised
        return {
            "ok": False,
            "model": checked["model"],
            "api_base": checked["api_base"],
            "error": str(exc)[:300],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "ok": True,
        "model": checked["model"],
        "api_base": checked["api_base"],
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
