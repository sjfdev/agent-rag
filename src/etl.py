import hashlib
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from src import chunking, embedding_cache
from src.config import settings
from src.engine import kb_engine
from src.hashing import file_sha1
from src.loaders import LEGACY_EXTENSIONS, SUPPORTED_EXTENSIONS, load_file

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = settings.MAX_UPLOAD_MB * 1024 * 1024
PREFETCH_MAX_DEPTH = 8
_END = object()


def prefetch_thread_target(cpus: Optional[int] = None) -> int:
    """Torch intra-op threads while a separate thread is parsing.

    The parser needs a full core of its own; leaving two cores free (parser +
    OS/vector-store work) measured ~20% faster than letting embedding claim
    every core and fight the parse thread for CPU.
    """
    total = cpus if cpus is not None else (os.cpu_count() or 1)
    return max(1, int(total) - 2)



@dataclass
class _Plan:
    """One file's preparation state.

    ``queued`` is decided once during planning (a changed file that needs a
    parse) so the consuming thread can find the matching prepared result even
    though the worker mutates ``status`` and the chunk lists.
    """

    dept: str
    path: Path
    source: str
    doc_id: str
    doc_hash: str = ""
    existing_hash: Optional[str] = None
    queued: bool = False
    # todo -> queued for parsing; skip/fail decided in planning; ready/empty/error
    # are set by the parser (and "ready" carries the chunks to write).
    status: str = "todo"
    reason: str = ""
    texts: List[str] = field(default_factory=list)
    metadatas: List[Dict] = field(default_factory=list)
    parse_ms: float = 0.0
    chunk_ms: float = 0.0


def doc_id_for(dept_name: str, source: str) -> str:
    """Stable document identity: sha1(dept|relative/source/path)."""
    return hashlib.sha1(f"{dept_name}|{source}".encode()).hexdigest()


class EnterpriseETL:
    """Multi-format, incremental ingestion with department and page metadata.

    A document is skipped when its stored hash (file sha1 plus the effective
    chunking/parser signature) already exists in the vector store, replaced
    when it changed, and pruned when the file is gone. Nested folders under a
    department are supported.

    With ``INGEST_PREFETCH`` enabled a worker thread parses and chunks the next
    changed document while the main thread embeds and writes the current one;
    parsing (PDF text extraction) and embedding are both CPU-heavy but on
    different threads, so they overlap instead of running one after the other.
    All vector-store access stays on the main thread, so rebuild/shadow-index
    behaviour and incremental skipping are unchanged.
    """

    def __init__(
        self,
        data_dir: str = "./data",
        on_log: Optional[Callable[[str], None]] = None,
        prefetch: Optional[bool] = None,
        prefetch_depth: Optional[int] = None,
    ):
        self.data_dir = Path(data_dir)
        self.on_log = on_log
        self.prefetch = bool(settings.INGEST_PREFETCH if prefetch is None else prefetch)
        depth = settings.INGEST_PREFETCH_DEPTH if prefetch_depth is None else prefetch_depth
        self.prefetch_depth = max(1, min(int(depth), PREFETCH_MAX_DEPTH))
        # Chunking is token-aware and driven by the effective runtime config
        # (admin console overrides included). The signature is folded into
        # doc_hash so a parameter change re-chunks existing documents.
        # 分片按 token 计算，并读取管理台覆盖；签名并入 doc_hash，
        # 参数变化后下次增量入库会自动重切。
        self.chunk_config = chunking.load_config()
        self.chunk = chunking.summary(self.chunk_config)
        self.stats = {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "pruned": 0,
            "failed": 0,
            "chunks": 0,
            "timings_ms": {"parse": 0.0, "chunk": 0.0, "embed": 0.0, "write": 0.0},
        }

    def chunk_description(self) -> str:
        """One-line summary for ingestion logs and the console."""
        item = self.chunk
        return (
            f"tokens={item['chunk_tokens']} overlap={item['chunk_overlap_tokens']} "
            f"chinese_punctuation={item['chinese_punctuation']} "
            f"title_prefix={item['title_prefix']} signature={item['signature']} "
            f"tokenizer={item['tokenizer']}"
        )

    def timing_description(self) -> str:
        """Aggregate phase timings for the job log and monitor."""
        timings = self.stats.get("timings_ms") or {}
        return (
            f"parse={int(timings.get('parse', 0))}ms "
            f"chunk={int(timings.get('chunk', 0))}ms "
            f"embed={int(timings.get('embed', 0))}ms "
            f"write={int(timings.get('write', 0))}ms"
        )

    def pipeline_description(self) -> str:
        """How documents are processed, for logs and the console."""
        if not self.prefetch:
            return "sequential (parse -> embed -> write per file)"
        return (
            f"prefetch depth={self.prefetch_depth} "
            "(parse/chunk of the next file overlaps embed/write of the current one)"
        )

    def _record_timing(
        self, parse_ms: float, chunk_ms: float, embed_ms: float, write_ms: float
    ) -> None:
        timings = self.stats["timings_ms"]
        timings["parse"] += max(0.0, parse_ms)
        timings["chunk"] += max(0.0, chunk_ms)
        timings["embed"] += max(0.0, embed_ms)
        timings["write"] += max(0.0, write_ms)

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
        files = self._iter_files(folder)
        if not files:
            logger.warning("no supported files for dept=%s in %s", dept_name, folder)
            return set()

        # Planning is cheap (hashes are cached by path+mtime+size) and must stay
        # in file order: it decides skip/parse before any parsing is queued.
        plans = [self._plan_file(dept_name, path) for path in files]
        seen = {plan.doc_id for plan in plans}
        todo = [plan for plan in plans if plan.queued]

        if self.prefetch and len(todo) > 1:
            self._log(f"  prefetch  depth={self.prefetch_depth}")
            self._run_pipeline(plans)
        else:
            for plan in plans:
                if plan.queued:
                    plan = self._parse(plan)
                self._consume(plan)
        return seen

    def _plan_file(self, dept_name: str, path: Path) -> _Plan:
        """Decide whether a file is skipped, fails early, or needs parsing."""
        source = self._source_name(path)
        plan = _Plan(
            dept=dept_name,
            path=path,
            source=source,
            doc_id=doc_id_for(dept_name, source),
        )
        size = path.stat().st_size
        if size == 0:
            plan.status = "fail"
            plan.reason = "empty file"
            return plan
        if size > MAX_FILE_BYTES:
            plan.status = "fail"
            plan.reason = f"> {MAX_FILE_BYTES // (1024 * 1024)}MB"
            return plan

        # Fold the chunking signature into the stored hash: a chunking change
        # must invalidate the incremental skip even though the file is identical.
        # 把分片签名并入 doc_hash：只改分片参数也要触发重切。
        plan.doc_hash = chunking.document_hash(file_sha1(path), self.chunk_config)
        # During a shadow rebuild `get_doc_hash()` reads the (empty) shadow, so
        # every file is written; outside a rebuild it reads the live index.
        plan.existing_hash = kb_engine.get_doc_hash(plan.doc_id)
        if plan.existing_hash == plan.doc_hash:
            plan.status = "skip"
            return plan
        plan.queued = True
        return plan

    def _parse(self, plan: _Plan) -> _Plan:
        """Parse and chunk one document (safe to run on a worker thread)."""
        parse_started = time.perf_counter()
        try:
            sections = load_file(plan.path)
        except Exception as exc:
            logger.exception("failed to parse %s", plan.source)
            plan.status = "error"
            plan.reason = f"{exc.__class__.__name__}: {exc}"
            return plan
        plan.parse_ms = (time.perf_counter() - parse_started) * 1000

        texts: List[str] = []
        metadatas: List[Dict] = []
        index = 0
        chunk_started = time.perf_counter()
        try:
            for section in sections:
                meta = section.metadata or {}
                # Fixed key set with sentinels: the Milvus schema is derived from
                # the first inserted batch, so every chunk must carry every field.
                base = {
                    "dept": plan.dept,
                    "source": plan.source,
                    "doc_id": plan.doc_id,
                    "doc_hash": plan.doc_hash,
                    "kind": meta.get("kind") or plan.path.suffix.lstrip(".").lower(),
                    "page": meta.get("page") if isinstance(meta.get("page"), int) else 0,
                    "slide": meta.get("slide") if isinstance(meta.get("slide"), int) else 0,
                    "sheet": meta.get("sheet") or "",
                    "title": meta.get("title") or "",
                }
                # Split one page/slide/sheet block with the token-aware splitter.
                # The title prefix (when enabled) is added per chunk and its token
                # cost is reserved inside the configured window.
                for chunk in chunking.split_section(
                    section.page_content, base, self.chunk_config
                ):
                    meta_chunk = dict(chunk.metadata, chunk_index=index)
                    index += 1
                    texts.append(chunk.page_content)
                    metadatas.append(meta_chunk)
        except Exception as exc:
            logger.exception("failed to chunk %s", plan.source)
            plan.status = "error"
            plan.reason = f"{exc.__class__.__name__}: {exc}"
            return plan
        plan.chunk_ms = (time.perf_counter() - chunk_started) * 1000

        if not texts:
            plan.status = "empty"
            return plan
        plan.status = "ready"
        plan.texts = texts
        plan.metadatas = metadatas
        return plan

    def _consume(self, plan: _Plan) -> str:
        """Log a decision and, for a prepared document, embed and write it."""
        if plan.status == "skip":
            self.stats["skipped"] += 1
            self._log(f"  skip     {plan.source} (unchanged)")
            return plan.doc_id
        if plan.status == "fail":
            self.stats["failed"] += 1
            self._log(f"  skip     {plan.source} ({plan.reason})")
            return plan.doc_id
        if plan.status == "empty":
            self.stats["failed"] += 1
            self._log(f"  skip     {plan.source} (no extractable text)")
            return plan.doc_id
        if plan.status == "error":
            self.stats["failed"] += 1
            self._log(f"  error    {plan.source} ({plan.reason})")
            return plan.doc_id

        # Write the new chunks first, then delete older versions of this doc.
        # A crash in between leaves the old copy searchable (duplicate results
        # at worst) instead of losing the document entirely.
        if plan.existing_hash is not None:
            self.stats["updated"] += 1
            action = "update"
        else:
            self.stats["added"] += 1
            action = "add"

        source = plan.source
        write_started = time.perf_counter()
        embedding_cache.reset_document_timer()
        kb_engine.add_documents(plan.texts, plan.metadatas)
        if plan.existing_hash is not None:
            stale = kb_engine.delete_stale_versions(plan.doc_id, plan.doc_hash)
            if stale:
                self._log(f"  replaced {source}: removed {stale} old chunk(s)")
        write_total_ms = (time.perf_counter() - write_started) * 1000
        embed_ms = min(write_total_ms, embedding_cache.document_embedding_ms())
        write_ms = max(0.0, write_total_ms - embed_ms)
        self._record_timing(plan.parse_ms, plan.chunk_ms, embed_ms, write_ms)
        self.stats["chunks"] += len(plan.texts)
        self._log(
            f"  {action:<8} {source} ({len(plan.texts)} chunks) "
            f"[parse={int(plan.parse_ms)}ms chunk={int(plan.chunk_ms)}ms "
            f"embed={int(embed_ms)}ms write={int(write_ms)}ms]"
        )
        return plan.doc_id

    def _cap_embedding_threads(self) -> Optional[int]:
        """Leave CPU headroom for the parse worker; returns the previous value.

        Torch's intra-op pool otherwise claims every core and both phases end
        up ~30% slower, which makes the overlap a net loss.
        """
        try:
            import torch
        except Exception:  # torch is optional (fake / remote embeddings)
            return None
        previous = int(torch.get_num_threads())
        target = prefetch_thread_target()
        if target >= previous:
            return None
        torch.set_num_threads(target)
        return previous

    def _restore_embedding_threads(self, previous: Optional[int]) -> None:
        if previous is None:
            return
        try:
            import torch

            torch.set_num_threads(previous)
        except Exception:  # never fail an ingestion over a tuning detail
            pass

    def _run_pipeline(self, plans: List[_Plan]) -> None:
        """Consume prepared documents in file order while parsing runs ahead.

        The worker only parses/chunks; every vector-store call (hash lookup,
        write, delete of stale versions) stays on this (main) thread. The
        queue is bounded so at most ``prefetch_depth`` documents are held in
        memory as prepared chunks.
        """
        out: "queue.Queue" = queue.Queue(maxsize=self.prefetch_depth)

        def produce() -> None:
            try:
                for plan in plans:
                    if plan.queued:
                        out.put(self._parse(plan))
            except Exception:  # defensive: never leave the consumer waiting
                logger.exception("ingest prefetch worker failed")
            finally:
                out.put(_END)

        previous_threads = self._cap_embedding_threads()
        worker = threading.Thread(target=produce, name="ingest-prefetch", daemon=True)
        worker.start()
        worker_gone = False
        try:
            for plan in plans:
                if plan.queued:
                    if worker_gone:
                        prepared = self._parse(plan)
                    else:
                        prepared = out.get()
                        if prepared is _END:
                            # Worker died early: fall back to inline parsing so
                            # a document is never silently dropped.
                            logger.error("prefetch worker ended early; parsing inline")
                            worker_gone = True
                            prepared = self._parse(plan)
                    self._consume(prepared)
                else:
                    self._consume(plan)
            # Unblock the worker if the bounded queue is still full.
            while not worker_gone:
                if out.get() is _END:
                    break
        finally:
            worker.join(timeout=1)
            self._restore_embedding_threads(previous_threads)

    def _process_file(self, dept_name: str, path: Path) -> str:
        """Ingest a single file (used by tests and callers that bypass depts)."""
        plan = self._plan_file(dept_name, path)
        if plan.queued:
            plan = self._parse(plan)
        return self._consume(plan)

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
