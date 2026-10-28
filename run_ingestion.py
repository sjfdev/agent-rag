"""Ingest all department PDFs under ./data into the configured vector store."""

import os

from src.config import settings
from src.etl import EnterpriseETL


def main():
    etl = EnterpriseETL()
    root = "./data"
    departments = [
        d for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and d not in {"etc"}
    ]

    if not departments:
        print(f"No department folders found under {root}")
        return

    print(f"Vector store: {settings.VECTOR_STORE_TYPE}")
    for dept in sorted(departments):
        etl.process_department_folder(dept, os.path.join(root, dept))


if __name__ == "__main__":
    main()
