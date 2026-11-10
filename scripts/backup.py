"""Write a timestamped, checksummed backup of everything the service owns.

    python scripts/backup.py                       # default: BACKUP_DIR
    python scripts/backup.py --dest /mnt/backups
    python scripts/backup.py --live                # copy while Milvus runs (best effort)
    python scripts/backup.py --verify backups/agent-rag-backup-....tar.gz

The archive contains:

  * ``data/``    the source documents (the only copy that cannot be rebuilt)
  * ``config/``  local accounts (password hashes) and the optional ACL rules
  * ``logs/``    ingestion job history and the admin audit trail
  * ``vector/``  the live index: ``DB_PATH`` for FAISS, or
                 ``volumes{,-prod}/{milvus,etcd}`` for Milvus

Every archive carries ``manifest.json`` (the original absolute path of each
entry, index metadata, file counts) and ``SHA256SUMS`` (one hash per archived
file), plus a ``<archive>.sha256`` sidecar. ``--verify`` checks all of it without
writing anything.

Milvus and etcd must be stopped for a consistent vector-store copy: RocksDB
always has writes in flight, so a tar of a running server is not guaranteed to
recover. The script refuses to archive those paths while their containers are
up unless ``--live`` is passed (explicitly accepting a best-effort copy).
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ARCHIVE_PREFIX = "agent-rag-backup-"
DEFAULT_PROBE_URL = f"http://127.0.0.1:{os.environ.get('APP_PORT', '8000')}/info"
EXCLUDED_DIRS = {"__pycache__", ".git", ".pytest_cache", ".ruff_cache", "node_modules"}
EXCLUDED_FILES = {".DS_Store"}
VECTOR_KINDS = {"milvus", "etcd"}


class BackupError(RuntimeError):
    """A backup could not be produced safely."""


@dataclass
class Target:
    """One filesystem path in the archive and where it came from."""

    kind: str
    source: Path
    archive: str
    is_dir: bool


def _resolve(value) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _iter_members(root: Path):
    """Yield (path, relative-posix-path) for regular files and directories.

    Symlinks, sockets and the usual build/test junk are skipped so restoring
    never follows a link out of the restore target.
    """
    if root.is_symlink() or not root.exists():
        return
    if root.is_file():
        yield root, ""
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        relative = "" if base == root else base.relative_to(root).as_posix()
        yield base, relative
        dirnames[:] = sorted(
            name for name in dirnames if name not in EXCLUDED_DIRS and not name.startswith(".")
        )
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink() or name in EXCLUDED_FILES or not path.is_file():
                continue
            yield path, f"{relative}/{name}" if relative else name


def _arcname(prefix: str, relative: str) -> str:
    return prefix if not relative else f"{prefix}/{relative}"


def _tar_filter(info: tarfile.TarInfo):
    parts = Path(info.name).parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return None
    if Path(info.name).name in EXCLUDED_FILES:
        return None
    return info


def _sidecar(archive: Path) -> Path:
    return archive.with_name(archive.name + ".sha256")


def running_vector_containers() -> list:
    """Names of running Milvus/etcd containers, or [] when Docker is unavailable."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [name for name in names if "milvus" in name.lower() or "etcd" in name.lower()]


def service_reachable(url: str, timeout: float = 3.0) -> bool:
    """True when anything answers at `url`; a 401/503 still means the process is up."""
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _vector_name(path: Path) -> str:
    name = path.name.lower()
    return name if name in VECTOR_KINDS else "vector"


def _default_vector_targets(warnings: list) -> list:
    from src.config import settings

    explicit = settings.BACKUP_VECTOR_PATHS.strip()
    if explicit:
        targets = []
        used = set()
        for index, raw in enumerate(part.strip() for part in explicit.split(",")):
            if not raw:
                continue
            path = _resolve(raw)
            if not path.exists():
                warnings.append(f"vector path {path} does not exist; skipped")
                continue
            kind = _vector_name(path)
            prefix = f"vector/{kind}"
            if prefix in used:
                prefix = f"vector/{kind}-{index}"
            used.add(prefix)
            targets.append(Target(kind, path, prefix, path.is_dir()))
        return targets

    if settings.VECTOR_STORE_TYPE == "faiss":
        index = _resolve(settings.DB_PATH)
        if not index.exists():
            warnings.append(f"FAISS index {index} does not exist; skipped")
            return []
        return [Target("faiss", index, "vector/faiss", index.is_dir())]

    volume_root = None
    for name in ("volumes-prod", "volumes"):
        candidate = ROOT / name
        if (candidate / "milvus").exists():
            volume_root = candidate
            break
    if volume_root is None:
        warnings.append("no Milvus volume directory found (volumes-prod/ or volumes/)")
        return []
    targets = []
    for kind in ("milvus", "etcd"):
        path = volume_root / kind
        if path.exists():
            targets.append(Target(kind, path, f"vector/{kind}", path.is_dir()))
        else:
            warnings.append(f"Milvus volume {path} does not exist; skipped")
    return targets


def _state_targets(paths, kind: str, prefix: str) -> list:
    """Group account/ACL/job files by parent directory.

    A configured file living directly in the repository root must not drag the
    whole repository into the archive, so those are archived individually.
    """
    targets = []
    groups = {}
    for raw in paths:
        path = _resolve(raw)
        if not path.exists():
            continue
        parent = path.parent
        if parent.resolve() == ROOT:
            targets.append(Target(kind, path, f"{prefix}/{path.name}", False))
        else:
            groups.setdefault(parent, []).append(path)
    for index, parent in enumerate(sorted(groups), 1):
        archive = prefix if index == 1 else f"{prefix}-{index}"
        targets.append(Target(kind, parent, archive, True))
    return targets


def default_targets() -> tuple:
    """Collect what should go into the archive and any skipped-path warnings."""
    from src.config import settings

    warnings = []
    targets = []

    data_dir = _resolve(settings.DATA_DIR)
    if data_dir.exists():
        targets.append(Target("data", data_dir, "data", data_dir.is_dir()))
    else:
        warnings.append(f"data directory {data_dir} does not exist; skipped")

    targets += _state_targets(
        [settings.AUTH_USERS_FILE, settings.ACL_FILE], "config", "config"
    )
    targets += _state_targets(
        [settings.JOBS_PATH, settings.AUDIT_LOG_PATH], "logs", "logs"
    )
    targets += _default_vector_targets(warnings)

    unique = []
    seen = set()
    for target in targets:
        key = target.source.resolve()
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique, warnings


def _scan_target(target: Target):
    """Return (SHA256SUMS lines, file count, bytes) for one target."""
    lines = []
    files = 0
    total = 0
    for path, relative in _iter_members(target.source):
        if not path.is_file():
            continue
        lines.append(f"{_sha256_file(path)}  {_arcname(target.archive, relative)}")
        files += 1
        total += path.stat().st_size
    return lines, files, total


def _build_manifest(targets: list, scans: list) -> dict:
    from src.config import settings
    from src.version import __version__

    embedding_model = (
        settings.LOCAL_EMBEDDING_MODEL
        if settings.EMBEDDING_BACKEND == "local"
        else settings.EMBEDDING_MODEL
    )
    entries = []
    for target, (_, files, total) in zip(targets, scans, strict=True):
        entries.append(
            {
                "kind": target.kind,
                "archive": target.archive,
                "path": str(target.source),
                "type": "dir" if target.is_dir else "file",
                "files": files,
                "bytes": total,
            }
        )
    return {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "version": __version__,
        "vector_store": settings.VECTOR_STORE_TYPE,
        "embedding": {"backend": settings.EMBEDDING_BACKEND, "model": embedding_model},
        "entries": entries,
        "files": sum(scan[1] for scan in scans),
        "bytes": sum(scan[2] for scan in scans),
    }


def _add_bytes(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(payload))


def _unique_archive(dest: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidate = dest / f"{ARCHIVE_PREFIX}{stamp}.tar.gz"
    index = 1
    while candidate.exists() or _sidecar(candidate).exists():
        candidate = dest / f"{ARCHIVE_PREFIX}{stamp}-{index}.tar.gz"
        index += 1
    return candidate


def create_backup(dest=None, live: bool = False, probe_url: str = DEFAULT_PROBE_URL) -> Path:
    """Write an archive and return its path; raise BackupError when unsafe."""
    from src.config import settings

    dest_path = _resolve(dest) if dest else _resolve(settings.BACKUP_DIR)
    dest_path.mkdir(parents=True, exist_ok=True)

    targets, warnings = default_targets()
    for warning in warnings:
        print(f"warning: {warning}")
    if not targets:
        raise BackupError("nothing to back up (check DATA_DIR, DB_PATH and config paths)")

    vector_kinds = {target.kind for target in targets} & VECTOR_KINDS
    if vector_kinds:
        running = running_vector_containers()
        if running and not live:
            raise BackupError(
                "vector-store containers are running ("
                + ", ".join(running)
                + "); stop them for a consistent copy (`make milvus-down`) "
                "or pass --live to accept a best-effort one"
            )

    if probe_url and service_reachable(probe_url):
        print(
            f"warning: API responds at {probe_url}; avoid ingesting while the backup runs"
        )

    scans = [_scan_target(target) for target in targets]
    manifest = _build_manifest(targets, scans)
    checksums = "\n".join(line for scan in scans for line in scan[0])
    if checksums:
        checksums += "\n"

    archive = _unique_archive(dest_path)
    partial = archive.with_name(archive.name + ".partial")
    try:
        with tarfile.open(partial, "w:gz") as tar:
            for target in targets:
                for path, relative in _iter_members(target.source):
                    tar.add(
                        str(path),
                        arcname=_arcname(target.archive, relative),
                        recursive=False,
                        filter=_tar_filter,
                    )
            _add_bytes(tar, "SHA256SUMS", checksums.encode("utf-8"))
            _add_bytes(
                tar,
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        partial.replace(archive)
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    digest = _sha256_file(archive)
    sidecar = _sidecar(archive)
    temporary = sidecar.with_name(sidecar.name + ".tmp")
    temporary.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    temporary.replace(sidecar)

    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"Backup written: {archive} ({size_mb:.1f} MB)")
    for entry in manifest["entries"]:
        print(f"  {entry['archive']:<14} {entry['files']:>5} file(s)  {entry['path']}")
    print(f"Checksum: {sidecar}")
    return archive


def _unsafe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return normalized.startswith("/") or ".." in normalized.split("/")


def read_manifest(tar: tarfile.TarFile) -> dict:
    """Load and validate manifest.json from an open archive."""
    try:
        handle = tar.extractfile("manifest.json")
    except KeyError:
        raise BackupError("archive has no manifest.json") from None
    if handle is None:
        raise BackupError("manifest.json is not a regular file")
    try:
        data = json.loads(handle.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"manifest.json is unreadable: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise BackupError("manifest.json is malformed")
    return data


def verify_archive(archive, quiet: bool = False) -> list:
    """Check the sidecar, member paths, manifest and every per-file hash.

    Returns a list of problems; empty means the archive is intact.
    """
    archive = Path(archive)
    errors = []
    if not archive.is_file():
        errors.append(f"archive not found: {archive}")
        if not quiet:
            for error in errors:
                print(f"ERROR: {error}")
        return errors

    sidecar = _sidecar(archive)
    if not sidecar.is_file():
        errors.append(f"checksum sidecar missing: {sidecar}")
    else:
        recorded = sidecar.read_text(encoding="utf-8").split()
        recorded = recorded[0] if recorded else ""
        actual = _sha256_file(archive)
        if recorded != actual:
            errors.append(
                f"checksum mismatch (recorded {recorded[:12]}..., actual {actual[:12]}...)"
            )

    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = {member.name: member for member in tar.getmembers()}
            unsafe = [name for name in members if _unsafe_member(name)]
            if unsafe:
                errors.append(f"unsafe member path: {unsafe[0]}")
                members = {}

            if not members:
                pass
            else:
                manifest = None
                try:
                    manifest = read_manifest(tar)
                except BackupError as exc:
                    errors.append(str(exc))

                if manifest is not None:
                    if manifest.get("schema") != 1:
                        errors.append(f"unsupported manifest schema: {manifest.get('schema')}")
                    for entry in manifest["entries"]:
                        prefix = entry.get("archive")
                        present = isinstance(prefix, str) and any(
                            name == prefix or name.startswith(prefix + "/") for name in members
                        )
                        if not present:
                            errors.append(f"manifest entry {prefix!r} is missing from the archive")

                if "SHA256SUMS" not in members:
                    errors.append("SHA256SUMS is missing from the archive")
                else:
                    payload = tar.extractfile(members["SHA256SUMS"])
                    lines = payload.read().decode("utf-8").splitlines() if payload else []
                    lines = [line for line in lines if line.strip()]
                    for line in lines:
                        try:
                            digest, name = line.split("  ", 1)
                        except ValueError:
                            errors.append(f"malformed SHA256SUMS line: {line!r}")
                            continue
                        member = members.get(name)
                        if member is None:
                            errors.append(f"SHA256SUMS references a missing member: {name}")
                            continue
                        handle = tar.extractfile(member)
                        if handle is None or _sha256_stream(handle) != digest:
                            errors.append(f"file hash mismatch: {name}")
                    if manifest is not None and manifest.get("files") != len(lines):
                        errors.append(
                            f"manifest file count {manifest.get('files')} != "
                            f"SHA256SUMS entries {len(lines)}"
                        )
    except (tarfile.TarError, OSError) as exc:
        errors.append(f"cannot read archive: {type(exc).__name__}: {exc}")

    if not quiet:
        for error in errors:
            print(f"ERROR: {error}")
        if not errors:
            print(f"Verify OK: {archive}")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", help="destination directory (default: BACKUP_DIR)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="copy even while Milvus/etcd containers run (accepts an inconsistent copy)",
    )
    parser.add_argument(
        "--probe-url",
        default=DEFAULT_PROBE_URL,
        help="API URL used to warn about writes during backup (empty disables)",
    )
    parser.add_argument("--verify", metavar="ARCHIVE", help="only verify an existing archive")
    args = parser.parse_args(argv)

    if args.verify:
        return 1 if verify_archive(args.verify, quiet=False) else 0
    try:
        create_backup(dest=args.dest, live=args.live, probe_url=args.probe_url)
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
