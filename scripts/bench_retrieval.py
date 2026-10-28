"""Benchmark retrieval latency: FAISS vs Milvus vs embedding vs LLM.

Usage:
    python scripts/bench_retrieval.py
"""

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
    samples = sorted(samples)
    p50 = samples[len(samples) // 2]
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    print(
        f"  {name:<22} mean={statistics.mean(samples)*1000:7.1f}ms "
        f"p50={p50*1000:7.1f}ms p95={p95*1000:7.1f}ms "
        f"min={min(samples)*1000:7.1f}ms"
    )


def bench_driver(name, driver):
    for q in QUERIES[:2]:
        driver.search(q, "Tech", k=4)
    samples = []
    for q in QUERIES:
        t = time.perf_counter()
        driver.search(q, "Tech", k=4)
        samples.append(time.perf_counter() - t)
    stats(name, samples)


def main():
    print("Building embeddings (local model)...")
    embeddings = build_embeddings()

    samples = []
    for q in QUERIES:
        t = time.perf_counter()
        embeddings.embed_query(q)
        samples.append(time.perf_counter() - t)
    stats("embedding only", samples)

    bench_driver("FAISS search", FAISSDriver(embeddings))

    try:
        bench_driver("Milvus search", MilvusDriver(embeddings))
    except Exception as e:
        print(f"  Milvus unavailable: {e}")

    # LLM latency (single completion, no tools)
    base = settings.LLM_API_BASE.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}"}
    payload = {
        "model": settings.LLM_MODEL,
        "messages": [{"role": "user", "content": "Reply with one short sentence about leave policy."}],
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
