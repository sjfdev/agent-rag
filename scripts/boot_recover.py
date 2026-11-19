"""Bring the service back after a host reboot or login (macOS launchd).

    python scripts/boot_recover.py
    python scripts/boot_recover.py --skip-monitoring

Starts colima when the Docker daemon is not running, waits for it, stops a
leftover dev stack (`docker-compose.yml`: `milvus-standalone`) that would
otherwise compete for the VM's memory with the production Milvus, then
`compose up -d` for the production and monitoring stacks and waits for
`/info` to answer. Once the app is live it runs the backup catch-up
(`daily_backup.py --max-age-hours 24`), which exits immediately unless the
newest archive is overdue, so a missed interval is repaired at the next
login/boot. The compose call is retried a few times because a hard VM
stop leaves Milvus needing longer than compose's dependency wait before it is
healthy. Exit codes: 0 healthy, 1 docker/compose failure, 2 app not live in
time.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROD_COMPOSE = ROOT / "docker-compose.prod.yml"
DEV_COMPOSE = ROOT / "docker-compose.yml"
MONITOR_COMPOSE = ROOT / "docker-compose.monitoring.yml"
DAILY_BACKUP = ROOT / "scripts" / "daily_backup.py"
DEV_MILVUS = "milvus-standalone"
LIVENESS_URL = "http://127.0.0.1:8000/info"


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def _run(cmd: Sequence[str], timeout: int = 600) -> int:
    try:
        proc = subprocess.run(
            list(cmd), cwd=str(ROOT), text=True, capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"command failed: {' '.join(cmd)}: {exc}")
        return 127
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "").strip()
        log(f"command failed ({proc.returncode}): {' '.join(cmd)}: {detail}")
    return proc.returncode


def _capture(cmd: Sequence[str], timeout: int = 60) -> str:
    """Run a read-only probe and return its stdout ("" when it fails)."""
    try:
        proc = subprocess.run(
            list(cmd), cwd=str(ROOT), text=True, capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode:
        return ""
    return proc.stdout


def dev_milvus_running(probe: Callable[[Sequence[str]], str]) -> bool:
    out = probe(["docker", "ps", "--filter", f"name={DEV_MILVUS}", "--format", "{{.Names}}"])
    return DEV_MILVUS in out


def wait_for(
    predicate: Callable[[], bool],
    timeout: float,
    interval: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        sleep(interval)
    return predicate()


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def recover(
    *,
    timeout: float = 240,
    monitoring: bool = True,
    retries: int = 3,
    retry_delay: float = 15.0,
    catch_up_backup: bool = True,
    backup_max_age_hours: float = 24,
    runner: Callable[[Sequence[str]], int] = _run,
    probe: Callable[[Sequence[str]], str] = _capture,
    liveness: Callable[[], bool] = lambda: http_ok(LIVENESS_URL),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    if runner(["docker", "info"]) != 0:
        log("docker daemon is not reachable; starting colima")
        if runner(["colima", "start"]) != 0:
            log("colima start failed")
            return 1
        if not wait_for(lambda: runner(["docker", "info"]) == 0, timeout, sleep=sleep):
            log("docker daemon did not come up in time")
            return 1
    log("docker daemon is up")

    # A leftover dev Milvus would be a second 5 GB instance in the same 8 GB VM
    # and both get OOM-killed (exit 137). Production owns the VM; drop the dev
    # stack instead of racing it.
    if dev_milvus_running(probe):
        log(f"dev Milvus ({DEV_MILVUS}) is running; stopping the dev stack first")
        if runner(["docker", "compose", "-f", str(DEV_COMPOSE), "down", "--remove-orphans"]) != 0:
            log("warning: could not stop the dev stack")

    for attempt in range(1, retries + 1):
        if runner(["docker", "compose", "-f", str(PROD_COMPOSE), "up", "-d"]) == 0:
            break
        if attempt == retries:
            log("could not start the production stack")
            return 1
        log(f"production stack did not start (attempt {attempt}/{retries}); retrying")
        sleep(retry_delay)
    log("production stack requested")

    if monitoring and runner(["docker", "compose", "-f", str(MONITOR_COMPOSE), "up", "-d"]) != 0:
        # Monitoring is optional: never fail the boot because of it.
        log("warning: could not start the monitoring stack")

    if not wait_for(liveness, timeout, sleep=sleep):
        log("app did not answer /info in time")
        return 2
    log("app is live")

    if catch_up_backup:
        # A no-op when the newest archive is fresh: the script exits 0 without
        # touching the stack. Never fail the boot because of the backup.
        log("checking whether a backup is overdue")
        if runner(
            [sys.executable, str(DAILY_BACKUP), "--max-age-hours", f"{backup_max_age_hours:g}"]
        ) != 0:
            log("warning: the catch-up backup failed")
        else:
            log("backup check done")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=240, help="seconds to wait (default 240)")
    parser.add_argument("--skip-monitoring", action="store_true", help="do not start the monitoring stack")
    args = parser.parse_args(argv)
    return recover(timeout=args.timeout, monitoring=not args.skip_monitoring)


if __name__ == "__main__":
    raise SystemExit(main())
