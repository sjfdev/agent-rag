"""Unit checks for hybrid retrieval (no vector store or LLM needed).

    python scripts/check_hybrid.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.documents import Document

from src.lexical import LexicalCache, LexicalIndex, rrf_fuse, tokenize

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail and not ok else ""))


def doc(text: str, doc_id: str, chunk: int, source: str = "a.pdf") -> Document:
    return Document(
        page_content=text,
        metadata={"doc_id": doc_id, "chunk_index": chunk, "source": source},
    )


def main() -> int:
    check("latin tokens", tokenize("VPN Access 2026") == ["vpn", "access", "2026"])
    zh = tokenize("访客密码")
    check("cjk unigrams", "访" in zh and "密" in zh)
    check("cjk bigrams", "访客" in zh and "密码" in zh)

    docs = [
        doc("Passwords rotate every 90 days.", "d1", 0),
        doc("Visitor Wi-Fi uses DEMO-GUEST at reception.", "d2", 0),
        doc("Annual leave is 20 days.", "d3", 0),
    ]
    index = LexicalIndex(docs)
    exact = index.search("DEMO-GUEST", 2)
    check("bm25 finds exact identifier", bool(exact) and exact[0].metadata["doc_id"] == "d2")
    check("bm25 ignores stopword-ish query", index.search("", 2) == [])
    check("bm25 returns nothing for absent term", index.search("kubernetes", 2) == [])

    vector = [docs[2], docs[0]]
    lexical = [docs[0], docs[1]]
    fused = rrf_fuse([vector, lexical], 3)
    keys = [(d.metadata["doc_id"], d.metadata["chunk_index"]) for d in fused]
    check("rrf deduplicates", len(keys) == len(set(keys)))
    check("rrf favours agreement", keys[0] == ("d1", 0), str(keys))
    check("rrf respects limit", len(rrf_fuse([vector, lexical], 1)) == 1)

    loads = {"n": 0}

    def loader(_dept: str):
        loads["n"] += 1
        return docs

    cache = LexicalCache()
    cache.get("Tech", 1, loader)
    cache.get("Tech", 1, loader)
    check("cache reused for same version", loads["n"] == 1, str(loads))
    cache.get("Tech", 2, loader)
    check("cache rebuilt on version bump", loads["n"] == 2, str(loads))
    cache.invalidate()
    cache.get("Tech", 2, loader)
    check("invalidate forces reload", loads["n"] == 3, str(loads))

    print(f"\nRESULT: {'OK' if not FAIL else 'FAILED'} - {len(PASS)} passed, {len(FAIL)} failed")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
