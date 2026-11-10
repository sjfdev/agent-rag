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
import time
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

    from src.health import storage_report

    storage = storage_report()
    disk_detail = (
        f"{storage['free_mb']} MB free at {storage['path']} "
        f"(min {storage['min_free_mb']} MB)"
    )
    if storage["ok"]:
        check("Disk space", OK, disk_detail)
    else:
        # A full disk does not fail queries; it silently breaks ingestion, and
        # Milvus caches the first ENOSPC until restarted. Catch it here.
        check("Disk space", FAIL, disk_detail)

    # --- backups ----------------------------------------------------------
    backup_dir = Path(settings.BACKUP_DIR)
    archives = sorted(
        backup_dir.glob("agent-rag-backup-*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not archives:
        check("Backup", WARN, f"no archive in {backup_dir} (run `make backup`)")
    else:
        newest = archives[0]
        age_days = (time.time() - newest.stat().st_mtime) / 86400
        sidecar = newest.with_name(newest.name + ".sha256")
        if age_days > settings.BACKUP_WARN_DAYS:
            check(
                "Backup",
                WARN,
                f"{newest.name}, {age_days:.0f} day(s) old "
                f"(threshold {settings.BACKUP_WARN_DAYS}; run `make backup`)",
            )
        elif not sidecar.is_file():
            check("Backup", WARN, f"{newest.name} has no checksum sidecar")
        else:
            check("Backup", OK, f"{newest.name}, {age_days:.0f} day(s) old")

    # --- access control ---------------------------------------------------
    from src import acl, local_auth

    acl_state = acl.status()
    if not acl_state["enabled"]:
        check("Document ACL", OK, "disabled (every document is public in its dept)")
    elif acl_state["ok"]:
        check("Document ACL", OK, f"{acl_state['rules']} rule(s)")
    else:
        # Fails closed at runtime: no document is served. Almost always a
        # typo or a missing volume mount in production.
        check("Document ACL", FAIL, str(acl_state["error"]))

    if local_auth.enabled():
        users = local_auth.list_users()
        admins = sum(1 for user in users if user["admin"] and not user["disabled"])
        if not users:
            check(
                "Local accounts",
                FAIL,
                f"enabled but {settings.AUTH_USERS_FILE} has no accounts "
                "(create one with scripts/manage_users.py)",
            )
        elif not admins:
            check("Local accounts", FAIL, f"{len(users)} account(s) but no enabled admin")
        else:
            check(
                "Local accounts",
                OK,
                f"{len(users)} account(s), {admins} admin, {settings.AUTH_USERS_FILE}",
            )
    else:
        check("Local accounts", OK, "disabled")

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
