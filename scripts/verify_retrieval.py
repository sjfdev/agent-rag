"""Offline retrieval check: no LLM endpoint required.

Verifies that department metadata filtering prevents cross-department leakage.
Run after `python run_ingestion.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.engine import kb_engine

CASES = [
    ("Tech", "What is required before merging code?", "coding"),
    ("Tech", "What are the salary bands in the 2026 budget?", "budget"),
    ("Finance", "What are the salary bands in the 2026 budget?", "budget"),
    ("Finance", "When must expense claims be submitted?", "expense"),
    ("HR", "How many annual leave days do employees get?", "leave"),
    ("Demo", "How do I request VPN access?", "vpn"),
    ("Demo", "What is the test coverage metric?", "coverage"),
]


def main():
    leaked = False
    for dept, query, _marker in CASES:
        results = kb_engine.search(query, dept, k=3)
        print(f"\n[{dept}] {query}")
        if not results:
            print("  (no results)")
            continue
        for doc in results:
            src = (doc.metadata.get("source") or "").split("/")[-1]
            dept_tag = doc.metadata.get("dept")
            print(f"  - {dept_tag} | {src} | {doc.page_content[:70].strip()!r}")
            if dept_tag != dept:
                leaked = True
                print("    !! PERMISSION LEAK")

    # Explicit isolation assertion: Tech must never see the Finance budget doc.
    tech_budget = kb_engine.search("salary bands 2026 budget", "Tech", k=10)
    if any("budget" in (d.metadata.get("source") or "").lower() for d in tech_budget):
        leaked = True
        print("\nFAILED: Tech user can see Finance budget document")

    # Multi-department users see exactly their departments, nothing else.
    multi = kb_engine.search("policy", ["Tech", "HR"], k=10)
    multi_depts = {d.metadata.get("dept") for d in multi}
    print(f"\n[Tech+HR] multi-department -> {sorted(multi_depts)}")
    if not multi_depts <= {"Tech", "HR"}:
        leaked = True
        print("FAILED: multi-department search leaked another department")

    print("\nRESULT:", "FAILED - permission leak detected" if leaked else "OK - isolation holds")


if __name__ == "__main__":
    main()
