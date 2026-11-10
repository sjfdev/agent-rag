"""Restore an archive produced by scripts/backup.py.

    python scripts/restore.py backups/agent-rag-backup-....tar.gz
    python scripts/restore.py backups/... --dry-run
    python scripts/restore.py backups/... --discard-old
    python scripts/restore.py backups/... --force        # service still running

Safety rules:

  * the archive is fully verified (sidecar, manifest, every file hash) before
    anything on disk is touched;
  * restore refuses to run while the API answers or while Milvus/etcd containers
    are up, unless ``--force`` is passed;
  * extraction happens in a staging directory, and each existing target is
    renamed to ``<name>.pre-restore-<timestamp>`` before the new copy is moved
    into place (``--discard-old`` deletes the previous copy afterwards);
  * a failure halfway through rolls back the entries already swapped.

Manifest paths are absolute: this restores to the same host/layout the backup
was taken from.
"""

import argparse
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup  # noqa: E402  (local helper module in scripts/)

DEFAULT_PROBE_URL = backup.DEFAULT_PROBE_URL


class RestoreError(RuntimeError):
    """The archive cannot be restored safely."""


def service_reachable(url: str, timeout: float = 3.0) -> bool:
    return backup.service_reachable(url, timeout)


def _free_bytes(path: Path):
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _sibling(target: Path, suffix: str) -> Path:
    candidate = target.with_name(target.name + suffix)
    index = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = target.with_name(f"{target.name}{suffix}-{index}")
        index += 1
    return candidate


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _plan(manifest: dict) -> list:
    plans = []
    seen = set()
    used_archives = set()
    for entry in manifest["entries"]:
        archive_name = entry.get("archive")
        raw_target = entry.get("path")
        if not isinstance(archive_name, str) or not isinstance(raw_target, str):
            raise RestoreError("manifest contains an invalid entry")
        if archive_name in used_archives:
            raise RestoreError(f"manifest repeats archive entry {archive_name!r}")
        used_archives.add(archive_name)
        target = Path(raw_target)
        if not target.is_absolute():
            target = (backup.ROOT / target).resolve()
        key = str(target)
        if key in seen:
            raise RestoreError(f"manifest restores the same path twice: {target}")
        seen.add(key)
        plans.append(
            {
                "archive": archive_name,
                "target": target,
                "kind": entry.get("kind", "unknown"),
                "is_dir": entry.get("type", "dir") == "dir",
                "files": entry.get("files"),
            }
        )
    return plans


def restore_archive(
    archive,
    probe_url: str = DEFAULT_PROBE_URL,
    force: bool = False,
    dry_run: bool = False,
    keep_old: bool = True,
) -> dict:
    """Verify and restore an archive; return a summary of what happened."""
    archive = Path(archive)
    errors = backup.verify_archive(archive, quiet=True)
    if errors:
        raise RestoreError("archive failed verification: " + "; ".join(errors))

    try:
        with tarfile.open(archive, "r:gz") as tar:
            manifest = backup.read_manifest(tar)
    except (tarfile.TarError, OSError) as exc:
        raise RestoreError(f"cannot read archive: {type(exc).__name__}: {exc}") from exc

    plans = _plan(manifest)
    if not plans:
        raise RestoreError("archive contains no entries")
    needed = int(manifest.get("bytes") or 0)

    if not force:
        if service_reachable(probe_url):
            raise RestoreError(
                f"the API is reachable at {probe_url}; stop it before restoring "
                "(or pass --force if you are sure)"
            )
        vector_kinds = {plan["kind"] for plan in plans} & backup.VECTOR_KINDS
        if vector_kinds:
            running = backup.running_vector_containers()
            if running:
                raise RestoreError(
                    "vector-store containers are running ("
                    + ", ".join(running)
                    + "); stop them before restoring (`make milvus-down`)"
                )

    parents = {plan["target"].parent for plan in plans}
    for parent in parents:
        free = _free_bytes(parent)
        if free is not None and free < needed:
            raise RestoreError(
                f"not enough free space at {parent}: "
                f"{free / (1024 * 1024):.0f} MB available, "
                f"{needed / (1024 * 1024):.0f} MB needed"
            )

    if dry_run:
        print(
            f"Dry run OK: {archive} "
            f"({manifest.get('files', '?')} file(s), {needed / (1024 * 1024):.1f} MB)"
        )
        for plan in plans:
            print(f"  {plan['archive']:<14} -> {plan['target']}")
        return {"archive": str(archive), "entries": plans, "restored": False}

    staging = Path(tempfile.mkdtemp(prefix="agent-rag-restore-"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    swapped = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if backup._unsafe_member(member.name):
                    raise RestoreError(f"unsafe member path: {member.name}")
                if member.issym() or member.islnk() or member.isdev():
                    raise RestoreError(f"refusing link/device member: {member.name}")
            try:
                tar.extractall(staging, filter="data")
            except TypeError:  # pragma: no cover - Python without PEP 706 filters
                tar.extractall(staging)

        for plan in plans:
            staged = staging / plan["archive"]
            if not staged.exists() and not staged.is_symlink():
                raise RestoreError(f"archive member is missing: {plan['archive']}")
            if plan["is_dir"] and not staged.is_dir():
                raise RestoreError(f"expected a directory for {plan['archive']}")
            if not plan["is_dir"] and not staged.is_file():
                raise RestoreError(f"expected a file for {plan['archive']}")

        for plan in plans:
            staged = staging / plan["archive"]
            target = plan["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            old = None
            if target.exists() or target.is_symlink():
                old = _sibling(target, f".pre-restore-{stamp}")
                target.rename(old)
            shutil.move(str(staged), str(target))
            swapped.append((target, old))
    except Exception as exc:
        for target, old in reversed(swapped):
            try:
                if target.exists() or target.is_symlink():
                    failed = _sibling(target, f".failed-restore-{stamp}")
                    target.rename(failed)
                if old is not None:
                    old.rename(target)
            except OSError:
                pass
        if isinstance(exc, RestoreError):
            raise
        raise RestoreError(f"restore failed: {type(exc).__name__}: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if not keep_old:
        for _, old in swapped:
            if old is None:
                continue
            try:
                _remove_path(old)
            except OSError as exc:
                print(f"warning: could not remove {old}: {exc}")

    print(f"Restored {len(swapped)} entr{'y' if len(swapped) == 1 else 'ies'} from {archive}")
    for target, old in swapped:
        kept = f"  (previous copy: {old})" if old is not None and keep_old else ""
        print(f"  {target}{kept}")
    print()
    print("Restart the API (and Milvus, if you restored its volumes), then verify:")
    print("  python scripts/verify_retrieval.py")
    print("  python scripts/eval_retrieval.py")
    return {
        "archive": str(archive),
        "entries": plans,
        "restored": True,
        "previous": [(str(target), None if old is None else str(old)) for target, old in swapped],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="?", help="archive produced by scripts/backup.py")
    parser.add_argument("--force", action="store_true", help="restore even if the API is running")
    parser.add_argument("--dry-run", action="store_true", help="verify and list targets only")
    parser.add_argument(
        "--discard-old",
        action="store_true",
        help="delete the previous copy instead of keeping <name>.pre-restore-<timestamp>",
    )
    parser.add_argument(
        "--probe-url",
        default=DEFAULT_PROBE_URL,
        help="API URL checked before restoring (empty disables)",
    )
    args = parser.parse_args(argv)
    if not args.archive:
        parser.error("an archive path is required")

    try:
        restore_archive(
            args.archive,
            probe_url=args.probe_url,
            force=args.force,
            dry_run=args.dry_run,
            keep_old=not args.discard_old,
        )
    except RestoreError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
