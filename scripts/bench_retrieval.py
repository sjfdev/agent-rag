"""Benchmark retrieval latency: embedding, FAISS, Milvus, reranking and LLM.

Usage:
    python scripts/bench_retrieval.py
    python scripts/bench_retrieval.py --no-llm          # skip the LLM probe
    python scripts/bench_retrieval.py --rerank          # add the rerank comparison
    python scripts/bench_retrieval.py --queries 20      # repeat the query set

The rerank comparison is skipped automatically when `RERANK_ENABLED=false`
unless `--rerank` is passed; it also reports the model load time separately so
the first-request cost is not mistaken for per-query latency.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src.config import settings
from src.engine import FAISSDriver, MilvusDriver, build_embeddings

QUERIES = [
    "What are the code review requirements?",
    "How many annual leave days do I get?",
    "When must expense claims be submitted?",
    "What is the 2026 operating budget?",
    "Can I carry over unused leave?",
    "What is the on-call rotation?",
    "How do I report a security incident?",
    "What is the travel reimbursement limit?",
    "Who approves expense claims?",
    "What are the branch naming rules?",
]


def stats(name, samples):
    ordered = sorted(samples)
    p50 = ordered[len(ordered) // 2]
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    print(
        f"  {name:<22} mean={statistics.mean(ordered) * 1000:7.1f}ms "
        f"p50={p50 * 1000:7.1f}ms p95={p95 * 1000:7.1f}ms "
        f"min={min(ordered) * 1000:7.1f}ms"
    )


def _queries(repeat):
    out = []
    for _ in range(max(repeat, 1)):
        out.extend(QUERIES)
    return out


def bench_driver(name, driver, queries, dept="Tech"):
    for q in queries[:2]:
        driver.search(q, dept, k=4)
    samples = []
    for q in queries:
        t = time.perf_counter()
        driver.search(q, dept, k=4)
        samples.append(time.perf_counter() - t)
    stats(name, samples)


def bench_rerank(queries, candidates=12, top_k=4):
    """Compare first-stage retrieval with and without a Cross-Encoder pass."""
    from src.engine import get_engine
    from src.reranker import (
        EnterpriseReranker,
        rerank_documents,  # noqa: F401  (API surface check)
    )

    engine = get_engine()

    def first_stage(query):
        return engine.driver.search(query, "Tech", candidates)

    # Warm the retriever, then measure the baseline.
    for q in queries[:2]:
        first_stage(q)
    baseline = []
    candidates_cache = []
    for q in queries:
        t = time.perf_counter()
        docs = first_stage(q)
        baseline.append(time.perf_counter() - t)
        candidates_cache.append(docs)

    print(f"  rerank model       {settings.RERANK_MODEL}")
    t = time.perf_counter()
    reranker = EnterpriseReranker()
    loaded = reranker._ensure_model()
    load_ms = (time.perf_counter() - t) * 1000
    if loaded is None:
        print("  rerank             SKIPPED (model unavailable; cache it first)")
        return
    print(f"  rerank load        {load_ms:7.1f}ms")

    # Warm the model once so the comparison is per-query steady state.
    reranker.rerank(queries[0], candidates_cache[0], top_k)

    rerank_samples = []
    total_samples = []
    for q, docs in zip(queries, candidates_cache, strict=False):
        t = time.perf_counter()
        reranker.rerank(q, docs, top_k)
        rerank_samples.append(time.perf_counter() - t)
        # End-to-end: first stage + rerank, which is what a request pays.
        t = time.perf_counter()
        first_stage(q)
        reranker.rerank(q, docs, top_k)
        total_samples.append(time.perf_counter() - t)

    stats("rerank only", rerank_samples)
    stats("retrieval+rerank", total_samples)
    stats("retrieval only", baseline)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-llm", action="store_true", help="skip the LLM probe")
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="force the rerank comparison even if RERANK_ENABLED is false",
    )
    parser.add_argument("--queries", type=int, default=1, help="repeat the query set N times")
    parser.add_argument("--dept", default="Tech", help="department to query")
    args = parser.parse_args()

    queries = _queries(args.queries)

    print("Building embeddings (local model)...")
    embeddings = build_embeddings()

    samples = []
    for q in queries:
        t = time.perf_counter()
        embeddings.embed_query(q)
        samples.append(time.perf_counter() - t)
    stats("embedding only", samples)

    bench_driver("FAISS search", FAISSDriver(embeddings), queries, args.dept)

    try:
        bench_driver("Milvus search", MilvusDriver(embeddings), queries, args.dept)
    except Exception as e:
        print(f"  Milvus unavailable: {e}")

    if args.rerank or settings.RERANK_ENABLED:
        try:
            bench_rerank(queries)
        except Exception as e:
            print(f"  rerank benchmark skipped: {e}")

    if args.no_llm:
        print("  LLM completion      SKIPPED (--no-llm)")
        return

    base = settings.LLM_API_BASE.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}"}
    payload = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "user", "content": "Reply with one short sentence about leave policy."}
        ],
        "temperature": 0,
        "max_tokens": 120,
    }
    samples = []
    for _ in range(3):
        t = time.perf_counter()
        r = requests.post(f"{base}/chat/completions", headers=headers, json=payload, timeout=120)
        r.raise_for_status()
        samples.append(time.perf_counter() - t)
    stats("LLM completion", samples)


if __name__ == "__main__":
    main()
