import hashlib
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.config import settings
from src.engine import kb_engine
from src.hashing import file_sha1
from src.loaders import LEGACY_EXTENSIONS, SUPPORTED_EXTENSIONS, load_file

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = settings.MAX_UPLOAD_MB * 1024 * 1024


def doc_id_for(dept_name: str, source: str) -> str:
    """Stable document identity: sha1(dept|relative/source/path)."""
    return hashlib.sha1(f"{dept_name}|{source}".encode()).hexdigest()


class EnterpriseETL:
    """Multi-format, incremental ingestion with department and page metadata.

    A document is skipped when its sha1 content hash already exists in the
    vector store, replaced when the hash changed, and pruned when the file is
    gone. Nested folders under a department are supported.
    """

    def __init__(
        self,
        data_dir: str = "./data",
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.data_dir = Path(data_dir)
        self.on_log = on_log
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=800,
            chunk_overlap=100,
            separators=["\n\n", "\n", " ", ""],
        )
        self.stats = {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "pruned": 0,
            "failed": 0,
            "chunks": 0,
        }

    def _log(self, message: str) -> None:
        print(message)
        if self.on_log:
            try:
                self.on_log(message)
            except Exception:  # never let a logging callback break ingestion
                pass

    def _source_name(self, path: Path) -> str:
        try:
            return path.relative_to(self.data_dir).as_posix()
        except ValueError:
            return path.name

    def _iter_files(self, folder: Path) -> List[Path]:
        files = []
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.name.startswith((".", "~$")):
                continue
            suffix = path.suffix.lower()
            if suffix in LEGACY_EXTENSIONS:
                self._log(
                    f"  skip     {self._source_name(path)} "
                    f"(legacy {suffix}; convert to {LEGACY_EXTENSIONS[suffix]})"
                )
                continue
            if suffix in SUPPORTED_EXTENSIONS:
                files.append(path)
        return files

    def process_department_folder(self, dept_name: str, folder_path: str) -> Set[str]:
        """Ingest every supported file in a folder for one department.

        Returns the doc ids seen on disk so callers can prune deleted files.
        """
        folder = Path(folder_path)
        seen: Set[str] = set()
        files = self._iter_files(folder)
        if not files:
            logger.warning("no supported files for dept=%s in %s", dept_name, folder)
            return seen

        for path in files:
            doc_id = self._process_file(dept_name, path)
            if doc_id:
                seen.add(doc_id)
        return seen

    def _process_file(self, dept_name: str, path: Path) -> str:
        source = self._source_name(path)
        size = path.stat().st_size
        doc_id = doc_id_for(dept_name, source)

        if size == 0:
            self._log(f"  skip     {source} (empty file)")
            self.stats["failed"] += 1
            return doc_id
        if size > MAX_FILE_BYTES:
            self._log(f"  skip     {source} (> {MAX_FILE_BYTES // (1024 * 1024)}MB)")
            self.stats["failed"] += 1
            return doc_id

        doc_hash = file_sha1(path)
        # During a shadow rebuild `get_doc_hash()` reads the (empty) shadow, so
        # every file is written; outside a rebuild it reads the live index.
        existing_hash = kb_engine.get_doc_hash(doc_id)
        if existing_hash == doc_hash:
            self.stats["skipped"] += 1
            self._log(f"  skip     {source} (unchanged)")
            return doc_id

        # Load first: a parse failure must not delete the previously indexed copy.
        try:
            sections = load_file(path)
        except Exception as exc:
            logger.exception("failed to parse %s", source)
            self._log(f"  error    {source} ({exc.__class__.__name__}: {exc})")
            self.stats["failed"] += 1
            return doc_id

        texts: List[str] = []
        metadatas: List[Dict] = []
        index = 0
        for section in sections:
            meta = section.metadata or {}
            # Fixed key set with sentinels: the Milvus schema is derived from
            # the first inserted batch, so every chunk must carry every field.
            base = {
                "dept": dept_name,
                "source": source,
                "doc_id": doc_id,
                "doc_hash": doc_hash,
                "kind": meta.get("kind") or path.suffix.lstrip(".").lower(),
                "page": meta.get("page") if isinstance(meta.get("page"), int) else 0,
                "slide": meta.get("slide") if isinstance(meta.get("slide"), int) else 0,
                "sheet": meta.get("sheet") or "",
                "title": meta.get("title") or "",
            }
            for chunk in self.splitter.split_documents([section]):
                meta_chunk = dict(base, chunk_index=index)
                index += 1
                texts.append(chunk.page_content)
                metadatas.append(meta_chunk)

        if not texts:
            self._log(f"  skip     {source} (no extractable text)")
            self.stats["failed"] += 1
            return doc_id

        # Write the new chunks first, then delete older versions of this doc.
        # A crash in between leaves the old copy searchable (duplicate results
        # at worst) instead of losing the document entirely.
        if existing_hash is not None:
            self.stats["updated"] += 1
            action = "update"
        else:
            self.stats["added"] += 1
            action = "add"

        kb_engine.add_documents(texts, metadatas)
        if existing_hash is not None:
            stale = kb_engine.delete_stale_versions(doc_id, doc_hash)
            if stale:
                self._log(f"  replaced {source}: removed {stale} old chunk(s)")
        self.stats["chunks"] += len(texts)
        self._log(f"  {action:<8} {source} ({len(texts)} chunks)")
        return doc_id

    def prune(self, keep: Set[str]) -> int:
        """Delete chunks whose source file no longer exists under ./data."""
        if kb_engine.is_rebuilding:
            # The shadow index only contains current files; pruning live ids
            # here would be wrong and could delete freshly written chunks.
            return 0
        stale = sorted(kb_engine.list_doc_ids() - keep)
        if not stale:
            return 0
        removed = kb_engine.delete_by_doc_ids(stale)
        # Count documents rather than chunks or round-trips.
        self.stats["pruned"] += len(stale)
        self._log(f"  pruned   {len(stale)} doc(s), {removed} chunk(s)")
        return self.stats["pruned"]


# Example usage script
if __name__ == "__main__":
    import os

    etl = EnterpriseETL()
    os.makedirs("./data/HR", exist_ok=True)
    os.makedirs("./data/Finance", exist_ok=True)

    # Example: etl.process_department_folder("HR", "./data/HR")
