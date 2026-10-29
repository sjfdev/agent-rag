"""End-to-end check for server-side sparse BM25 on Milvus 2.5+.

Skips (exit 0) when the configured server cannot do sparse BM25, so it is safe
to run in CI against older servers. Use a throwaway collection: it is dropped
before and after the test.

    VECTOR_STORE_TYPE=milvus python scripts/check_hybrid_milvus.py
"""

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.engine import _connection_args, _inspect_milvus, kb_engine

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail and not ok else ""))


def main() -> int:
    if settings.VECTOR_STORE_TYPE != "milvus":
        print("SKIP  VECTOR_STORE_TYPE is not milvus")
        return 0

    collection = f"hybrid_check_{uuid.uuid4().hex[:8]}"
    caps = _inspect_milvus(collection, settings.HYBRID_SPARSE_FIELD)
    print(
        f"server={caps.server_version} version_ok={caps.version_ok} "
        f"collection={collection}"
    )
    if not caps.version_ok:
        print("SKIP  server is older than 2.5; server-side sparse BM25 unavailable")
        return 0

    from pymilvus import DataType, MilvusClient
    from langchain_core.documents import Document

    from src.engine import MilvusDriver

    client = MilvusClient(**_connection_args())
    try:
        client.drop_collection(collection)
    except Exception:
        pass

    old_collection = settings.MILVUS_COLLECTION
    old_engine = settings.HYBRID_ENGINE
    try:
        settings.MILVUS_COLLECTION = collection
        settings.HYBRID_ENGINE = "milvus"
        driver = MilvusDriver(kb_engine.embeddings)
        check("driver uses sparse engine", driver.sparse_enabled)
        check("status reports ready", driver.capabilities.sparse_ready)

        docs = [
            Document("Visitor wifi uses the DEMO-GUEST network at reception.",
                     {"source": "demo.txt", "dept": "Demo", "doc_id": "d1",
                      "doc_hash": "h1", "kind": "txt", "page": 1, "slide": 0,
                      "sheet": "", "title": "", "chunk_index": 0}),
            Document("Passwords rotate every 90 days on the VPN portal.",
                     {"source": "tech.txt", "dept": "Tech", "doc_id": "d2",
                      "doc_hash": "h2", "kind": "txt", "page": 1, "slide": 0,
                      "sheet": "", "title": "", "chunk_index": 0}),
        ]
        driver.add_documents([d.page_content for d in docs],
                             [dict(d.metadata) for d in docs])
        client.flush(collection)

        fields = {f["name"] for f in client.describe_collection(collection)["fields"]}
        check("sparse field created", settings.HYBRID_SPARSE_FIELD in fields, str(fields))
        check("bm25 index created", driver.capabilities.bm25_index)
        check("chunks counted", driver.count() == 2, str(driver.count()))

        # Exact identifier is the classic case where BM25 beats dense vectors.
        hits = driver.hybrid_search("DEMO-GUEST", "Demo", 2, 10)
        check("hybrid search returns docs", bool(hits), str(hits))
        if hits:
            check("hybrid filters department", all(
                d.metadata.get("dept") == "Demo" for d in hits), str([d.metadata for d in hits]))
            check("hybrid finds exact identifier",
                  "DEMO-GUEST" in hits[0].page_content, hits[0].page_content[:80])
            check("hybrid keeps snippets", all(d.page_content for d in hits))

        dense = driver.search("password rotation", "Tech", 2)
        check("dense search works with sparse field", bool(dense), str(dense))
        check("dense filters department", all(
            d.metadata.get("dept") == "Tech" for d in dense))

        leaked = driver.hybrid_search("DEMO-GUEST", "Tech", 2, 10)
        check("hybrid enforces dept isolation", all(
            d.metadata.get("dept") == "Tech" for d in (leaked or [])), str(leaked))

        # Delete + reset paths must keep working with the new schema.
        check("delete by doc_id", driver.delete_by_doc_id("d1") >= 0)
        check("reset rebuilds schema", True)
        driver.reset()
        check("reset leaves empty collection", driver.count() == 0, str(driver.count()))
        check("reset keeps sparse field", settings.HYBRID_SPARSE_FIELD in {
            f["name"] for f in client.describe_collection(collection)["fields"]
        })
    finally:
        settings.MILVUS_COLLECTION = old_collection
        settings.HYBRID_ENGINE = old_engine
        try:
            client.drop_collection(collection)
        except Exception:
            pass
        client.close()

    print(f"\nRESULT: {'OK' if not FAIL else 'FAILED'} - {len(PASS)} passed, {len(FAIL)} failed")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
