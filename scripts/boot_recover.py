"""Bring the service back after a host reboot or login (macOS launchd).

    python scripts/boot_recover.py
    python scripts/boot_recover.py --skip-monitoring

Starts colima when the Docker daemon is not running, waits for it, then
`compose up -d` for the production and monitoring stacks and waits for
`/info` to answer. Exit codes: 0 healthy, 1 docker/compose failure, 2 app not
live in time.
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
MONITOR_COMPOSE = ROOT / "docker-compose.monitoring.yml"
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
    runner: Callable[[Sequence[str]], int] = _run,
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

    if runner(["docker", "compose", "-f", str(PROD_COMPOSE), "up", "-d"]) != 0:
        log("could not start the production stack")
        return 1
    log("production stack requested")

    if monitoring and runner(["docker", "compose", "-f", str(MONITOR_COMPOSE), "up", "-d"]) != 0:
        # Monitoring is optional: never fail the boot because of it.
        log("warning: could not start the monitoring stack")

    if not wait_for(liveness, timeout, sleep=sleep):
        log("app did not answer /info in time")
        return 2
    log("app is live")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=240, help="seconds to wait (default 240)")
    parser.add_argument("--skip-monitoring", action="store_true", help="do not start the monitoring stack")
    args = parser.parse_args(argv)
    return recover(timeout=args.timeout, monitoring=not args.skip_monitoring)


if __name__ == "__main__":
    raise SystemExit(main())
