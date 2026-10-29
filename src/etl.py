import hashlib
import logging
from pathlib import Path
from typing import Dict, List, Set

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.engine import kb_engine

logger = logging.getLogger(__name__)


class EnterpriseETL:
    """PDF -> chunks with department, page and document identity metadata.

    Ingestion is incremental: a document is skipped when its sha1 content hash
    already exists in the vector store, replaced when the hash changed, and
    pruned when the file is gone.
    """

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        # Enterprise-grade splitting: balancing context and granularity
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=800,
            chunk_overlap=100,
            separators=["\n\n", "\n", " ", ""],
        )
        self.stats = {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "chunks": 0,
            "pruned": 0,
        }

    def _source_name(self, pdf: Path) -> str:
        try:
            return pdf.relative_to(self.data_dir).as_posix()
        except ValueError:
            return pdf.name

    def process_department_folder(self, dept_name: str, folder_path: str) -> Set[str]:
        """Ingest every PDF in a folder, tagging chunks with the department.

        Returns the doc ids seen on disk so callers can prune deleted files.
        """
        folder = Path(folder_path)
        seen: Set[str] = set()
        pdfs = sorted(folder.glob("*.pdf"))
        if not pdfs:
            logger.warning("no PDFs found for dept=%s in %s", dept_name, folder)
            return seen

        for pdf in pdfs:
            doc_id = self._process_file(dept_name, pdf)
            if doc_id:
                seen.add(doc_id)
        return seen

    def _process_file(self, dept_name: str, pdf: Path) -> str:
        source = self._source_name(pdf)
        doc_hash = hashlib.sha1(pdf.read_bytes()).hexdigest()
        doc_id = hashlib.sha1(f"{dept_name}|{source}".encode()).hexdigest()

        existing_hash = kb_engine.get_doc_hash(doc_id)
        if existing_hash == doc_hash:
            self.stats["skipped"] += 1
            print(f"  skip     {source} (unchanged)")
            return doc_id

        if existing_hash is not None:
            kb_engine.delete_by_doc_id(doc_id)
            self.stats["updated"] += 1
            action = "update"
        else:
            self.stats["added"] += 1
            action = "add"

        pages = PyPDFLoader(str(pdf)).load()
        chunks = self.splitter.split_documents(pages)

        texts: List[str] = []
        metadatas: List[Dict] = []
        for index, chunk in enumerate(chunks):
            meta = {
                "dept": dept_name,
                "source": source,
                "doc_id": doc_id,
                "doc_hash": doc_hash,
                "chunk_index": index,
            }
            page = chunk.metadata.get("page")
            if isinstance(page, int):
                meta["page"] = page + 1
            texts.append(chunk.page_content)
            metadatas.append(meta)

        if not texts:
            logger.warning("no extractable text in %s", source)
            print(f"  {action:<8} {source} (no text, skipped)")
            return doc_id

        kb_engine.add_documents(texts, metadatas)
        self.stats["chunks"] += len(texts)
        print(f"  {action:<8} {source} ({len(texts)} chunks)")
        return doc_id

    def prune(self, keep: Set[str]) -> int:
        """Delete chunks whose source file no longer exists under ./data."""
        stale = kb_engine.list_doc_ids() - keep
        for doc_id in sorted(stale):
            removed = kb_engine.delete_by_doc_id(doc_id)
            if removed:
                self.stats["pruned"] += 1
        return self.stats["pruned"]


# Example usage script
if __name__ == "__main__":
    import os

    etl = EnterpriseETL()
    # Structure: data/HR/*.pdf, data/Finance/*.pdf, etc.
    os.makedirs("./data/HR", exist_ok=True)
    os.makedirs("./data/Finance", exist_ok=True)

    # Example: etl.process_department_folder("HR", "./data/HR")
