"""Ingest all department PDFs under ./data into the configured vector store.

Incremental behaviour:
  * unchanged files are skipped (sha1 content hash stored in chunk metadata)
  * changed files replace their previous chunks
  * chunks of deleted files are pruned (disable with --no-prune)

Usage:
    python run_ingestion.py                 # incremental
    python run_ingestion.py --reset         # drop the index/collection first
    python run_ingestion.py --no-prune      # keep chunks of removed files
"""

import argparse
import os
import sys

from src.config import settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop the FAISS index / Milvus collection before ingesting",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="do not delete chunks whose source PDF was removed",
    )
    args = parser.parse_args()

    root = settings.DATA_DIR
    if not os.path.isdir(root):
        print(f"Missing data directory: {root}")
        return 1

    departments = sorted(
        d
        for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and d not in {"etc"}
    )
    if not departments:
        print(f"No department folders found under {root}")
        return 1

    print(f"Vector store: {settings.VECTOR_STORE_TYPE}")
    from src.engine import kb_engine
    from src.etl import EnterpriseETL

    if args.reset:
        # Build into a shadow index so a failed rebuild leaves the current one
        # intact. The live index keeps serving queries in the API process.
        print("Building a shadow index...")
        kb_engine.begin_rebuild()

    etl = EnterpriseETL(data_dir=root)
    keep = set()

    for dept in departments:
        print(f"\n[{dept}]")
        keep |= etl.process_department_folder(dept, os.path.join(root, dept))

    if args.reset and not keep:
        kb_engine.abort_rebuild()
        print("\nRebuild produced no documents; the previous index was kept.")
        return 1

    if not args.no_prune:
        pruned = etl.prune(keep)
        if pruned:
            print(f"\nPruned {pruned} document(s) no longer present under {root}.")

    if args.reset:
        print("\nSwitching over to the rebuilt index...")
        kb_engine.commit_rebuild()

    s = etl.stats
    print(
        f"\nDone. docs: added={s['added']} updated={s['updated']} "
        f"skipped={s['skipped']} pruned={s['pruned']}; chunks written={s['chunks']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
