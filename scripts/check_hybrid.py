"""Unit checks for hybrid retrieval (no vector store or LLM needed).

    python scripts/check_hybrid.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.documents import Document

import os
import tempfile

from src.engine import (
    MilvusCapabilities,
    MilvusDriver,
    hits_to_documents,
    kb_engine,
)
from src.hashing import clear_hash_cache, file_sha1, hash_cache_info
from src.hybrid import parse_version, resolve_hybrid_mode, version_at_least
from src.config import settings
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

    def racing_loader(_dept: str):
        loads["n"] += 1
        # Simulate an ingestion write completing mid-build.
        cache._version = 99
        return docs

    cache.get("Tech", 3, racing_loader)
    cache.get("Tech", 3, loader)
    check("stale build is not cached", loads["n"] == 5, str(loads))

    # --- capability adaptation ---
    check("parse v2.5.4", parse_version("v2.5.4") == (2, 5, 4))
    check("parse 2.5", parse_version("2.5") == (2, 5, 0))
    check("parse garbage", parse_version("unknown") is None)
    check("2.5.4 >= 2.5.0", version_at_least("v2.5.4", "2.5.0"))
    check("2.4.0 < 2.5.0", not version_at_least("v2.4.0", "2.5.0"))

    check("auto prefers sparse", resolve_hybrid_mode("auto", True, True) == "milvus")
    check("auto falls back", resolve_hybrid_mode("auto", True, False) == "local")
    check("local is honoured", resolve_hybrid_mode("local", True, True) == "local")
    check("milvus is honoured", resolve_hybrid_mode("milvus", True, False) == "milvus")
    check("disabled wins", resolve_hybrid_mode("auto", False, True) == "off")
    try:
        resolve_hybrid_mode("bogus", True, True)
        check("invalid engine rejected", False)
    except ValueError:
        check("invalid engine rejected", True)

    # Capability decision logic without a live Milvus server.
    def decide(caps, engine):
        driver = MilvusDriver.__new__(MilvusDriver)
        driver.capabilities = caps
        driver.collection = "test"
        old = settings.HYBRID_ENGINE
        try:
            settings.HYBRID_ENGINE = engine
            return driver._decide_sparse()
        finally:
            settings.HYBRID_ENGINE = old

    old_enabled = settings.HYBRID_ENABLED
    try:
        settings.HYBRID_ENABLED = True
        ready = MilvusCapabilities(
            server_version="v2.5.4", version_ok=True,
            collection_exists=True, sparse_field=True, bm25_index=True,
        )
        old_collection = MilvusCapabilities(
            server_version="v2.5.4", version_ok=True,
            collection_exists=True, sparse_field=False, bm25_index=False,
        )
        v24 = MilvusCapabilities(server_version="v2.4.0", version_ok=False)
        fresh = MilvusCapabilities(server_version="v2.5.4", version_ok=True)

        check("capability ready", ready.sparse_ready)
        check("capability old schema", not old_collection.sparse_ready)
        check("capability fresh collection", fresh.sparse_ready)
        check("auto uses sparse", decide(ready, "auto") is True)
        check("auto falls back on 2.4", decide(v24, "auto") is False)
        check("auto falls back on old schema", decide(old_collection, "auto") is False)
        check("local forced", decide(ready, "local") is False)
        check("milvus forced on ready", decide(ready, "milvus") is True)
        for name, caps in (("2.4", v24), ("old schema", old_collection)):
            try:
                decide(caps, "milvus")
                check(f"explicit milvus rejects {name}", False)
            except RuntimeError as exc:
                check(f"explicit milvus rejects {name}", "reset" in str(exc) or ">= 2.5" in str(exc))
        settings.HYBRID_ENABLED = False
        try:
            check("disabled engine off", decide(ready, "auto") is False)
        finally:
            settings.HYBRID_ENABLED = True
    finally:
        settings.HYBRID_ENABLED = old_enabled

    hits = [
        {"id": 7, "distance": 0.1, "entity": {"text": "alpha", "source": "a.pdf", "dept": "Tech"}},
        {"text": "beta", "source": "b.pdf", "pk": 9},
    ]
    converted = hits_to_documents(hits)
    check("hit conversion count", len(converted) == 2)
    check("hit conversion text", converted[0].page_content == "alpha")
    check("hit conversion metadata", converted[0].metadata.get("source") == "a.pdf")
    check("hit conversion drops ids", "id" not in converted[0].metadata and "pk" not in converted[1].metadata)
    check("hit conversion empty", hits_to_documents([]) == [])

    # --- file hashing cache ---
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "doc.md"
        target.write_text("hello world")
        clear_hash_cache()
        first = file_sha1(target)
        before = hash_cache_info().hits
        second = file_sha1(target)
        check("hash is stable", first == second)
        check("hash is cached", hash_cache_info().hits == before + 1, str(hash_cache_info()))
        target.write_text("hello world!")
        # Force a distinct mtime so the test does not depend on filesystem
        # timestamp granularity.
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        changed = file_sha1(target)
        check("hash changes on write", changed != first)
        check("hash cache invalidated by write", hash_cache_info().misses >= 2)

    # --- batch delete expression escaping (Milvus) ---
    expr = MilvusDriver._doc_expr(["a", 'we"ird', "back\\slash"])
    check("doc expr uses in-list", expr.startswith("doc_id in [") and expr.endswith("]"), expr)
    check("doc expr escapes quotes", '\\"' in expr, expr)
    check("doc expr escapes backslash", "\\\\" in expr, expr)

    status = kb_engine.hybrid_status()
    check("status has engine", status["engine"] in {"off", "local", "milvus"}, str(status))
    check("status reports sparse", isinstance(status["sparse_ready"], bool), str(status))

    print(f"\nRESULT: {'OK' if not FAIL else 'FAILED'} - {len(PASS)} passed, {len(FAIL)} failed")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
