"""Offline retrieval evaluation: no LLM endpoint required.

Cases live in eval/cases.jsonl. Two kinds are supported:

- expected cases: ``expect_sources`` lists filename substrings; a hit means at
  least one of them appears in the top-k results.
- isolation cases: ``forbid_sources`` must never appear (permission leak).

Exit code is non-zero when the hit rate is below ``--min-hit`` or any forbidden
source is returned. Run against both backends after ingestion:

    VECTOR_STORE_TYPE=faiss python scripts/eval_retrieval.py
    VECTOR_STORE_TYPE=milvus python scripts/eval_retrieval.py
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.engine import kb_engine

DEFAULT_CASES = Path(__file__).resolve().parent.parent / "eval" / "cases.jsonl"


def load_cases(path: Path) -> list:
    cases = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def source_of(doc) -> str:
    return (doc.metadata.get("source") or "").replace("\\", "/")


def matches(source: str, markers: list) -> bool:
    return any(marker in source for marker in markers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--k", type=int, default=settings.RETRIEVAL_K)
    parser.add_argument(
        "--min-hit",
        type=float,
        default=0.8,
        help="minimum hit@k over expected cases (0-1)",
    )
    args = parser.parse_args()

    cases = load_cases(args.cases)
    print(
        f"backend={settings.VECTOR_STORE_TYPE} k={args.k} cases={len(cases)} "
        f"hybrid={settings.HYBRID_ENABLED}"
    )

    if settings.HYBRID_ENABLED:
        # Building BM25 indexes is a one-off cost per department; keep it out
        # of the latency numbers. The API warms them at startup too.
        started = time.perf_counter()
        warmed = kb_engine.warm_lexical({case["dept"] for case in cases})
        print(
            f"warmup: {', '.join(warmed) or '-'} "
            f"({(time.perf_counter() - started) * 1000:.0f}ms)"
        )

    hits = 0
    expected = 0
    leaks = 0
    reciprocal_ranks = []
    latencies = []

    for case in cases:
        start = time.perf_counter()
        docs = kb_engine.search(case["query"], case["dept"], k=args.k)
        latencies.append((time.perf_counter() - start) * 1000)
        sources = [source_of(doc) for doc in docs]

        if case.get("forbid_sources"):
            bad = [s for s in sources if matches(s, case["forbid_sources"])]
            if bad:
                leaks += 1
                print(f"LEAK  {case['id']}: {bad}")
            else:
                print(f"ok    {case['id']} (isolation)")

        if case.get("expect_sources"):
            expected += 1
            rank = next(
                (i + 1 for i, s in enumerate(sources) if matches(s, case["expect_sources"])),
                None,
            )
            if rank:
                hits += 1
                reciprocal_ranks.append(1 / rank)
                print(f"hit   {case['id']} rank={rank} {sources[0]}")
            else:
                print(f"MISS  {case['id']} top={sources[:3]}")

    hit_rate = hits / expected if expected else 1.0
    mrr = statistics.mean(reciprocal_ranks) if reciprocal_ranks else 0.0
    p50 = statistics.median(latencies) if latencies else 0.0
    p95 = (
        statistics.quantiles(latencies, n=20)[18]
        if len(latencies) >= 20
        else max(latencies or [0.0])
    )

    print(
        f"\nhit@{args.k}={hits}/{expected} ({hit_rate:.0%})  MRR={mrr:.2f}  "
        f"leaks={leaks}  latency p50={p50:.0f}ms p95={p95:.0f}ms"
    )

    failed = leaks > 0 or hit_rate < args.min_hit
    print("RESULT:", "FAILED" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
