import hashlib
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

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
    size: int = 0
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
        cancel_event: Optional[threading.Event] = None,
    ):
        self.data_dir = Path(data_dir)
        self.on_log = on_log
        self.prefetch = bool(settings.INGEST_PREFETCH if prefetch is None else prefetch)
        depth = settings.INGEST_PREFETCH_DEPTH if prefetch_depth is None else prefetch_depth
        self.prefetch_depth = max(1, min(int(depth), PREFETCH_MAX_DEPTH))
        # Cancellation is cooperative and only checked at document boundaries.
        # Never interrupt between add_documents() and delete_stale_versions():
        # that would leave both the old and the new version of one document in
        # the index, and the unchanged (new) doc_hash would make the next
        # incremental run skip the stale copy forever.
        # 取消是协作式的，且只在文档边界检查；绝不在“写新 chunk / 删旧版本”之间
        # 中断，否则新旧版本会永久共存，下一次增量还会因 doc_hash 相同而跳过。
        self.cancel_event = cancel_event or threading.Event()
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

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

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
        plans = self.plan_department_folder(dept_name, folder_path)
        if not plans:
            logger.warning("no supported files for dept=%s in %s", dept_name, folder)
            return set()

        seen = {plan.doc_id for plan in plans}
        todo = [plan for plan in plans if plan.queued]

        if self.prefetch and len(todo) > 1:
            self._log(f"  prefetch  depth={self.prefetch_depth}")
            self._run_pipeline(plans)
        else:
            for plan in plans:
                # Document boundary: the previous document is fully written (or
                # skipped) before this check, so a cancel never splits a doc.
                # 文档边界：上一文档已完整写入或跳过，取消不会把一篇文档切一半。
                if self.cancel_requested:
                    self._log("  cancel    requested; stopping at a document boundary")
                    break
                if plan.queued:
                    plan = self._parse(plan)
                self._consume(plan)
        return seen

    def plan_department_folder(
        self, dept_name: str, folder_path: str, *, assume_absent: bool = False
    ) -> List[_Plan]:
        """Hash and classify files without parsing or writing anything.

        Used by the pre-ingest estimate and by dry runs. ``assume_absent``
        makes every valid file look new, which is what a ``--reset`` rebuild
        would do (it writes into an empty shadow index).
        """
        folder = Path(folder_path)
        return [
            self._plan_file(dept_name, path, assume_absent=assume_absent)
            for path in self._iter_files(folder)
        ]

    def _plan_file(
        self, dept_name: str, path: Path, *, assume_absent: bool = False
    ) -> _Plan:
        """Decide whether a file is skipped, fails early, or needs parsing."""
        source = self._source_name(path)
        plan = _Plan(
            dept=dept_name,
            path=path,
            source=source,
            doc_id=doc_id_for(dept_name, source),
        )
        size = path.stat().st_size
        plan.size = size
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
        if assume_absent:
            # A reset rebuild writes into an empty shadow index; skip the
            # per-document lookup and mark every valid file as queued.
            plan.existing_hash = None
            plan.queued = True
            return plan
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

    def dry_run_plans(
        self, plans: List[_Plan], *, limit: int = 3
    ) -> Dict[str, Any]:
        """Measure parse + chunk + embed on the first ``limit`` changed docs.

        Nothing is written and no old version is deleted: a partially migrated
        index is worse than no migration at all. The report extrapolates the
        full incremental run from the measured per-document cost, so the
        console can show the real cost before a chunking change.
        只测量前 N 篇的解析/切分/嵌入耗时，不写库、不删旧版本：半迁移的索引比
        不迁移更糟。报告会按实测单篇成本外推整次入库。
        """
        queued = [plan for plan in plans if plan.queued]
        samples = queued[: max(1, int(limit))]
        per_doc: List[Dict[str, Any]] = []

        for plan in samples:
            started = time.perf_counter()
            prepared = self._parse(plan)
            if prepared.status == "ready":
                embed_started = time.perf_counter()
                # Embed only: the vector-store write path is never entered.
                kb_engine.embed_documents(prepared.texts)
                embed_ms = (time.perf_counter() - embed_started) * 1000
                total_ms = (time.perf_counter() - started) * 1000
                chunks = len(prepared.texts)
                per_doc.append(
                    {
                        "dept": prepared.dept,
                        "source": prepared.source,
                        "status": "ready",
                        "size": prepared.size,
                        "chunks": chunks,
                        "parse_ms": round(prepared.parse_ms, 1),
                        "chunk_ms": round(prepared.chunk_ms, 1),
                        "embed_ms": round(embed_ms, 1),
                        "total_ms": round(total_ms, 1),
                        "chunks_per_second": (
                            chunks / (total_ms / 1000.0) if total_ms > 0 else 0.0
                        ),
                    }
                )
            else:
                per_doc.append(
                    {
                        "dept": prepared.dept,
                        "source": prepared.source,
                        "status": prepared.status,
                        "reason": prepared.reason,
                        "size": prepared.size,
                        "chunks": 0,
                        "parse_ms": round(prepared.parse_ms, 1),
                        "chunk_ms": round(prepared.chunk_ms, 1),
                        "embed_ms": 0.0,
                        "total_ms": round((time.perf_counter() - started) * 1000, 1),
                        "chunks_per_second": 0.0,
                    }
                )

        usable = [item for item in per_doc if item["status"] == "ready"]
        sampled_chunks = sum(int(item["chunks"]) for item in usable)
        sampled_seconds = sum(float(item["total_ms"]) for item in usable) / 1000.0
        sampled = len(usable)
        avg_seconds = sampled_seconds / sampled if sampled else 0.0
        avg_chunks = sampled_chunks / sampled if sampled else 0.0
        # Extrapolate by bytes first: file order mixes 5-page notes with 300-page
        # PDFs, so "average per document" alone can miss the total badly. Bytes
        # are known from planning and correlate far better than document count.
        # 先按字节外推：文件顺序里小文档和大 PDF 混在一起，只按“篇均耗时”会严重
        # 失真；规划阶段已知文件大小，字节数与工作量的相关性更好。
        queued_bytes = sum(max(0, int(plan.size)) for plan in queued)
        sampled_bytes = sum(max(0, int(item["size"])) for item in usable)
        if sampled_bytes > 0 and queued_bytes > 0:
            estimated_chunks = int(
                round(sampled_chunks / sampled_bytes * queued_bytes)
            )
            estimated_seconds = sampled_seconds / sampled_bytes * queued_bytes
            basis = "sampled-bytes"
        else:
            estimated_chunks = int(round(avg_chunks * len(queued)))
            estimated_seconds = avg_seconds * len(queued)
            basis = "sampled-docs"
        return {
            "dry_run": True,
            "limit": max(1, int(limit)),
            "total_files": len(plans),
            "queued": len(queued),
            "skipped": sum(1 for plan in plans if plan.status == "skip"),
            "failed": sum(1 for plan in plans if plan.status == "fail"),
            "sampled": sampled,
            "sample_chunks": sampled_chunks,
            "sample_seconds": round(sampled_seconds, 2),
            "avg_seconds_per_doc": round(avg_seconds, 2),
            "avg_chunks_per_doc": round(avg_chunks, 1),
            "queued_bytes": queued_bytes,
            "sampled_bytes": sampled_bytes,
            "basis": basis,
            "chunks_per_second": (
                round(sampled_chunks / sampled_seconds, 2)
                if sampled_seconds > 0
                else 0.0
            ),
            "estimated_chunks": estimated_chunks,
            "estimated_seconds": round(estimated_seconds, 1),
            "estimated_minutes": (
                round(estimated_seconds / 60.0, 1) if estimated_seconds else 0.0
            ),
            "per_doc": per_doc,
        }

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
        stop = self.cancel_event

        def put(item: object) -> bool:
            """Put without ever blocking a cancellation forever.

            The bounded queue would otherwise keep the worker stuck in ``put``
            after the consumer walks away; retry briefly and then drop the item
            (prefetched chunks are never written without ``_consume``).
            """
            while True:
                try:
                    out.put(item, timeout=0.2)
                    return True
                except queue.Full:
                    if stop.is_set():
                        return False

        def produce() -> None:
            try:
                for plan in plans:
                    # Document boundary for the parser too: stop pulling new
                    # files once a cancel is requested. A parse already in
                    # flight is allowed to finish (it writes nothing).
                    if stop.is_set():
                        break
                    if plan.queued:
                        if not put(self._parse(plan)):
                            break
            except Exception:  # defensive: never leave the consumer waiting
                logger.exception("ingest prefetch worker failed")
            finally:
                put(_END)

        previous_threads = self._cap_embedding_threads()
        worker = threading.Thread(target=produce, name="ingest-prefetch", daemon=True)
        worker.start()
        worker_gone = False
        try:
            for plan in plans:
                if stop.is_set():
                    self._log("  cancel    requested; stopping at a document boundary")
                    break
                if plan.queued:
                    if worker_gone:
                        prepared = self._parse(plan)
                    else:
                        prepared = out.get()
                        if prepared is _END:
                            worker_gone = True
                            if stop.is_set():
                                # Cancelled while waiting: the prepared
                                # document was never written and is discarded.
                                break
                            # Worker died early: fall back to inline parsing so
                            # a document is never silently dropped.
                            logger.error("prefetch worker ended early; parsing inline")
                            prepared = self._parse(plan)
                        elif stop.is_set():
                            # Cancelled while waiting for the prefetch result:
                            # skip its write, the document is still untouched.
                            break
                    self._consume(prepared)
                else:
                    self._consume(plan)
            # Unblock the worker if the bounded queue is still full. On cancel
            # the worker drops blocked puts itself, so do not wait for _END.
            if not stop.is_set():
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
