"""One-shot environment and connectivity check.

    python scripts/doctor.py
    python scripts/doctor.py --no-llm     # skip the LLM probe

Checks: Python version, key packages, embedding backend, vector store, index
size, Milvus reachability, data directory, and (optionally) the LLM endpoint.
Exit code is non-zero only for problems that block startup.
"""

import argparse
import os
import shutil
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, WARN, FAIL = "OK", "WARN", "FAIL"
results = []


def check(name, status, detail=""):
    results.append((status, name))
    print(f"[{status:4}] {name}" + (f"  {detail}" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-llm", action="store_true", help="skip the LLM probe")
    args = parser.parse_args()

    from src.config import settings

    # --- Python -----------------------------------------------------------
    version = sys.version_info
    if version[:2] == (3, 11):
        check("Python version", OK, f"{version.major}.{version.minor}.{version.micro}")
    else:
        check(
            "Python version",
            FAIL,
            f"{version.major}.{version.minor} — this project needs 3.11",
        )

    # --- packages ---------------------------------------------------------
    required = [
        "torch", "numpy", "langchain", "langchain_core", "langchain_milvus",
        "langchain_huggingface", "faiss", "pymilvus", "fastapi", "pydantic",
        "prometheus_client", "rank_bm25",
    ]
    missing = []
    for name in required:
        try:
            __import__(name)
        except Exception as exc:  # pragma: no cover - environment dependent
            missing.append(f"{name} ({type(exc).__name__})")
    if missing:
        check("Packages", FAIL, ", ".join(missing))
    else:
        check("Packages", OK, f"{len(required)} imports")

    try:
        import numpy

        major = int(numpy.__version__.split(".")[0])
        check(
            "NumPy ABI",
            OK if major < 2 else FAIL,
            f"numpy {numpy.__version__}" + ("" if major < 2 else " — torch 2.2 needs <2"),
        )
    except Exception:
        pass

    # --- configuration ----------------------------------------------------
    check("Vector store", OK, settings.VECTOR_STORE_TYPE)
    check("Embedding backend", OK, settings.EMBEDDING_BACKEND)
    if settings.EMBEDDING_BACKEND == "local":
        cached = Path(
            os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")
        ) / "hub"
        model_dir = cached / ("models--" + settings.LOCAL_EMBEDDING_MODEL.replace("/", "--"))
        check(
            "Embedding model cached",
            OK if model_dir.is_dir() else WARN,
            str(model_dir) if model_dir.is_dir() else "first run will download it",
        )

    data_dir = Path(settings.DATA_DIR)
    if data_dir.is_dir():
        depts = [p.name for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
        check("Data directory", OK if depts else WARN, f"{len(depts)} department(s)")
    else:
        check("Data directory", FAIL, f"{data_dir} does not exist")

    # --- index ------------------------------------------------------------
    try:
        from src.engine import kb_engine

        count = kb_engine.live_count()
        status = OK if count > 0 else WARN
        check("Index", status, f"{count} chunks ({kb_engine.hybrid_mode} hybrid)")
    except Exception as exc:
        check("Index", FAIL, f"{type(exc).__name__}: {exc}")

    # --- Milvus -----------------------------------------------------------
    if settings.VECTOR_STORE_TYPE == "milvus":
        host = settings.MILVUS_HOST
        port = int(settings.MILVUS_PORT)
        try:
            with socket.create_connection((host, port), timeout=3):
                check("Milvus port", OK, f"{host}:{port}")
        except Exception as exc:
            check("Milvus port", FAIL, f"{host}:{port} unreachable ({type(exc).__name__})")
        try:
            from src.engine import kb_engine
            from src.hybrid import version_at_least

            status = kb_engine.hybrid_status()
            version = status.get("server_version")
            check(
                "Milvus version",
                OK if version_at_least(version, "2.5.0") else WARN,
                f"{version} (>=2.5 enables server-side sparse BM25)",
            )
        except Exception:
            pass

    if shutil.which("docker"):
        check("Docker CLI", OK, shutil.which("docker"))
    else:
        check("Docker CLI", WARN, "not found (only needed for Milvus)")

    # --- LLM --------------------------------------------------------------
    if args.no_llm:
        check("LLM endpoint", WARN, "skipped (--no-llm)")
    else:
        try:
            import requests

            base = settings.LLM_API_BASE.rstrip("/")
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                json={
                    "model": settings.LLM_MODEL,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
                timeout=min(settings.LLM_TIMEOUT, 10),
            )
            if resp.status_code < 400:
                check("LLM endpoint", OK, f"{settings.LLM_MODEL} @ {base}")
            else:
                check("LLM endpoint", FAIL, f"HTTP {resp.status_code}")
        except Exception as exc:
            check("LLM endpoint", WARN, f"{type(exc).__name__} (chat will fail)")

    failures = [name for status, name in results if status == FAIL]
    print()
    if failures:
        print(f"RESULT: FAILED — {', '.join(failures)}")
        return 1
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
