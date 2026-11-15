"""Consistent daily backup: pause the stack, archive, prune, copy offsite.

    python scripts/daily_backup.py
    python scripts/daily_backup.py --offsite /Volumes/kb-archive
    python scripts/daily_backup.py --dry-run

`make backup` refuses to copy a running Milvus. This wrapper performs the
stop/start dance so launchd can run it unattended (install_launchd.py). The
restart lives in a ``finally`` block: a failed backup never leaves the service
down.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROD_COMPOSE = ROOT / "docker-compose.prod.yml"
DEV_COMPOSE = ROOT / "docker-compose.yml"
DEV_CONTAINERS = ("milvus-etcd", "milvus-standalone")
ARCHIVE_PATTERN = "agent-rag-backup-*.tar.gz"
SILENCES_URL = "http://127.0.0.1:9093/api/v2/silences"

Runner = Callable[[Sequence[str]], int]
Capture = Callable[[Sequence[str]], str]


def prod_milvus_present() -> bool:
    """True when the production compose stack keeps its index in Milvus."""
    return (ROOT / "volumes-prod" / "milvus").exists()


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def _run(cmd: Sequence[str], timeout: int = 900) -> int:
    proc = subprocess.run(
        list(cmd), cwd=str(ROOT), text=True, capture_output=True, timeout=timeout
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "").strip()
        log(f"command failed ({proc.returncode}): {' '.join(cmd)}: {detail}")
    return proc.returncode


def _capture(cmd: Sequence[str], timeout: int = 60) -> str:
    proc = subprocess.run(
        list(cmd), cwd=str(ROOT), text=True, capture_output=True, timeout=timeout
    )
    return proc.stdout if proc.returncode == 0 else ""


def _load_backup_module():
    spec = importlib.util.spec_from_file_location(
        "agent_rag_backup", ROOT / "scripts" / "backup.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prune_archives(dest: Path, keep: int) -> list[str]:
    """Delete the oldest archives and sidecars, keeping the newest `keep`."""
    if keep <= 0:
        return []
    archives = sorted(
        path for path in dest.glob(ARCHIVE_PATTERN) if not path.name.endswith(".partial")
    )
    removed: list[str] = []
    for path in archives[: max(len(archives) - keep, 0)]:
        for candidate in (path, path.with_name(path.name + ".sha256")):
            if candidate.exists():
                candidate.unlink()
                removed.append(candidate.name)
    return removed


def silence_payload(minutes: int, now: Optional[datetime] = None) -> dict:
    """Silence the readiness alert for a planned backup window."""
    start = now or datetime.now(timezone.utc)
    end = start + timedelta(minutes=minutes)

    def fmt(moment: datetime) -> str:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "matchers": [
            {"name": "alertname", "value": "AgentRagHealthDown", "isRegex": False, "isEqual": True}
        ],
        "startsAt": fmt(start),
        "endsAt": fmt(end),
        "createdBy": "daily-backup",
        "comment": "planned backup window: the stack is paused",
    }


def create_silence(
    minutes: int, url: str = SILENCES_URL, timeout: float = 5.0
) -> bool:
    if minutes <= 0:
        return False
    request = urllib.request.Request(
        url,
        data=json.dumps(silence_payload(minutes)).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status in (200, 201)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def copy_offsite(archive: Path, dest: str) -> Path:
    """Copy the archive and sidecar, then verify the copy byte-for-byte."""
    target_dir = Path(dest).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / archive.name
    shutil.copy2(archive, target)
    sidecar = archive.with_name(archive.name + ".sha256")
    if sidecar.exists():
        shutil.copy2(sidecar, target_dir / sidecar.name)
    if sha256(archive) != sha256(target):
        raise RuntimeError(f"offsite copy checksum mismatch: {target}")
    return target


def run_daily(
    *,
    dest: Optional[Path] = None,
    keep: int = 14,
    offsite: str = "",
    stop: bool = True,
    dry_run: bool = False,
    silence_minutes: int = 15,
    runner: Runner = _run,
    capture: Capture = _capture,
    backup_module=None,
) -> int:
    from src.config import settings

    backup = backup_module or _load_backup_module()
    dest = dest or Path(settings.BACKUP_DIR)

    if dry_run:
        log(
            "dry run: "
            f"stop={'yes' if stop else 'no'} dest={dest} keep={keep} "
            f"offsite={offsite or '(skipped)'}"
        )
        return 0

    docker_up = runner(["docker", "info"]) == 0
    if docker_up and stop and prod_milvus_present():
        # Production pins VECTOR_STORE_TYPE=milvus in the compose file while the
        # host .env may describe the local FAISS dev index: archive the former.
        settings.VECTOR_STORE_TYPE = "milvus"
    dev_was_running: list[str] = []
    if docker_up and stop:
        running = capture(["docker", "ps", "--format", "{{.Names}}"])
        dev_was_running = [n for n in DEV_CONTAINERS if n in running.split()]

    failure: Optional[str] = None
    try:
        if docker_up and stop:
            if silence_minutes:
                if create_silence(silence_minutes):
                    log(f"silenced AgentRagHealthDown for {silence_minutes} minutes")
                else:
                    log("warning: could not silence Alertmanager; a health alert may fire")
            if runner(["docker", "compose", "-f", str(PROD_COMPOSE), "stop", "app", "milvus", "etcd"]):
                raise RuntimeError("could not stop the production stack")
            log("production stack paused")
            if dev_was_running and runner(["docker", "compose", "-f", str(DEV_COMPOSE), "stop"]):
                log("warning: could not stop the dev stack; backup.py may refuse")

        archive = Path(backup.create_backup(dest=dest))
        log(f"wrote {archive.name} ({archive.stat().st_size} bytes)")
        for name in prune_archives(dest, keep):
            log(f"pruned {name}")
        if offsite:
            target = copy_offsite(archive, offsite)
            log(f"offsite copy verified at {target}")
        else:
            log("offsite copy skipped (OFFSITE_BACKUP_DIR is empty)")
    except Exception as exc:  # noqa: BLE001 - logged and surfaced as exit code
        failure = str(exc)
        log(f"backup failed: {exc}")
    finally:
        if docker_up:
            if runner(["docker", "compose", "-f", str(PROD_COMPOSE), "up", "-d"]):
                log("CRITICAL: could not restart the production stack")
                failure = failure or "restart failed"
            else:
                log("production stack restarted")
            for name in dev_was_running:
                if runner(["docker", "start", name]):
                    log(f"CRITICAL: could not restart {name}")
                    failure = failure or f"restart failed: {name}"

    return 0 if failure is None else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    from src.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", help=f"archive directory (default {settings.BACKUP_DIR})")
    parser.add_argument(
        "--keep",
        type=int,
        default=settings.BACKUP_KEEP,
        help=f"archives to keep (default {settings.BACKUP_KEEP})",
    )
    parser.add_argument(
        "--offsite",
        default=settings.OFFSITE_BACKUP_DIR,
        help="extra copy destination (default OFFSITE_BACKUP_DIR)",
    )
    parser.add_argument(
        "--no-stop",
        action="store_true",
        help="do not pause the stack (inconsistent Milvus copy; backup.py may refuse)",
    )
    parser.add_argument(
        "--silence-minutes",
        type=int,
        default=15,
        help="silence the readiness alert for this long (0 disables, default 15)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args(argv)
    dest = Path(args.dest) if args.dest else None
    return run_daily(
        dest=dest,
        keep=args.keep,
        offsite=args.offsite,
        stop=not args.no_stop,
        dry_run=args.dry_run,
        silence_minutes=args.silence_minutes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
