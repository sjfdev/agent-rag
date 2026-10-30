"""Health endpoint smoke test: no LLM token is spent unless --probe is passed.

    python scripts/check_health.py
    python scripts/check_health.py --probe --url http://127.0.0.1:8000
"""

import argparse
import sys

import requests

REQUIRED = {
    "status", "vector_store", "ingestion", "llm", "maintenance", "time", "hybrid",
    "cache",
}
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail and not ok else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--probe", action="store_true", help="also call the LLM")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    try:
        r = requests.get(f"{base}/health", timeout=10)
    except Exception as exc:
        print(f"FAIL  cannot reach {base}/health: {exc}")
        return 1

    check("HTTP 200", r.status_code == 200, f"got {r.status_code}: {r.text[:160]}")
    data = r.json()
    check("required fields", REQUIRED <= data.keys(), str(sorted(REQUIRED - data.keys())))

    store = data.get("vector_store") or {}
    check("vector store ok", bool(store.get("ok")), str(store.get("error")))
    chunks = store.get("chunks")
    check("index is not empty", isinstance(chunks, int) and chunks > 0, f"chunks={chunks}")
    check("status is ok", data.get("status") == "ok", f"status={data.get('status')}")
    check("no rebuild in progress", data.get("maintenance") is False)

    ingestion = data.get("ingestion") or {}
    check("ingestion idle", ingestion.get("active") is False)

    if r.status_code == 200:
        check("rerank flag present", "rerank_enabled" in data)

    hybrid = data.get("hybrid") or {}
    check(
        "hybrid engine known",
        hybrid.get("engine") in {"off", "local", "milvus"},
        str(hybrid),
    )
    check(
        "hybrid sparse flag is bool",
        isinstance(hybrid.get("sparse_ready"), bool),
        str(hybrid),
    )
    if hybrid.get("configured") == "milvus":
        check("explicit milvus is ready", bool(hybrid.get("sparse_ready")), str(hybrid))

    cache = data.get("cache") or {}
    check("cache status present", isinstance(cache.get("enabled"), bool), str(cache))
    answer = cache.get("answer") or {}
    check(
        "answer cache counters",
        all(isinstance(answer.get(k), int) for k in ("entries", "hits", "misses")),
        str(cache),
    )

    if args.probe:
        try:
            p = requests.get(f"{base}/health?probe=1", timeout=30).json()
        except Exception as exc:
            check("llm probe", False, str(exc))
        else:
            llm = p.get("llm") or {}
            check("llm probe", llm.get("probe") == "ok", str(llm))
            check("probe latency reported", isinstance(llm.get("latency_ms"), (int, float)))

    print(f"\nRESULT: {'OK' if not FAIL else 'FAILED'} - {len(PASS)} passed, {len(FAIL)} failed")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
