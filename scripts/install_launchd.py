"""Install the macOS launchd agents: boot recovery and the periodic backup.

    python scripts/install_launchd.py install
    python scripts/install_launchd.py status
    python scripts/install_launchd.py uninstall

Two LaunchAgents, both in ~/Library/LaunchAgents (they start at login: colima
needs the user session, so a root LaunchDaemon is the wrong hook):

  com.agent-rag.boot-recover   scripts/boot_recover.py at login
  com.agent-rag.daily-backup   scripts/daily_backup.py every `--interval-hours`
                               (default 6) and at login

The backup uses `StartInterval` instead of `StartCalendarInterval`: a calendar
job at 03:30 never fired on this machine (`launchctl print` showed
`runs = 0`) because it stays asleep at night, whereas an interval job is
coalesced by launchd and runs on the next wake. Every firing is cheap when
nothing is due — `daily_backup.py --max-age-hours 24` exits immediately
without pausing the stack unless the newest archive is older than 24 hours.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOGS_DIR = ROOT / "logs"
LAUNCHCTL = "/bin/launchctl"
PATH_VALUE = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BOOT_LABEL = "com.agent-rag.boot-recover"
BACKUP_LABEL = "com.agent-rag.daily-backup"


def boot_payload(python: str, logs_dir: Path = LOGS_DIR) -> dict:
    return {
        "Label": BOOT_LABEL,
        "ProgramArguments": [python, str(ROOT / "scripts" / "boot_recover.py")],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "StandardOutPath": str(logs_dir / "boot-agent.log"),
        "StandardErrorPath": str(logs_dir / "boot-agent.err.log"),
        "EnvironmentVariables": {"PATH": PATH_VALUE},
    }


def backup_payload(
    python: str,
    logs_dir: Path = LOGS_DIR,
    interval_hours: float = 6,
    keep: Optional[int] = None,
    max_age_hours: float = 24,
) -> dict:
    # No --keep means the job follows BACKUP_KEEP from .env.
    arguments = [
        python,
        str(ROOT / "scripts" / "daily_backup.py"),
        "--max-age-hours",
        f"{max_age_hours:g}",
    ]
    if keep is not None:
        arguments += ["--keep", str(keep)]
    return {
        "Label": BACKUP_LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(ROOT),
        # Interval, not a calendar time: the job is coalesced and runs on wake.
        "StartInterval": int(interval_hours * 3600),
        # Also check once at login, which is when this laptop comes back.
        "RunAtLoad": True,
        "StandardOutPath": str(logs_dir / "backup-agent.log"),
        "StandardErrorPath": str(logs_dir / "backup-agent.err.log"),
        "EnvironmentVariables": {"PATH": PATH_VALUE},
    }


def render_plist(payload: dict) -> bytes:
    return plistlib.dumps(payload, sort_keys=True)


def plist_paths(agents_dir: Path = AGENTS_DIR) -> dict[str, Path]:
    return {
        BOOT_LABEL: agents_dir / f"{BOOT_LABEL}.plist",
        BACKUP_LABEL: agents_dir / f"{BACKUP_LABEL}.plist",
    }


def _launchctl(*args: str) -> int:
    proc = subprocess.run([LAUNCHCTL, *args], text=True, capture_output=True)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "").strip()
        print(f"launchctl {' '.join(args)} -> {proc.returncode}: {detail}")
    return proc.returncode


def install(
    python: str,
    interval_hours: float = 6,
    keep: Optional[int] = None,
    max_age_hours: float = 24,
    agents_dir: Path = AGENTS_DIR,
    logs_dir: Path = LOGS_DIR,
) -> int:
    logs_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        BOOT_LABEL: boot_payload(python, logs_dir),
        BACKUP_LABEL: backup_payload(python, logs_dir, interval_hours, keep, max_age_hours),
    }
    domain = f"gui/{os.getuid()}"
    for label, payload in payloads.items():
        path = plist_paths(agents_dir)[label]
        path.write_bytes(render_plist(payload))
        if _launchctl("print", f"{domain}/{label}") == 0:
            _launchctl("bootout", domain, str(path))
        if _launchctl("bootstrap", domain, str(path)) != 0:
            return 1
        _launchctl("enable", f"{domain}/{label}")
        print(f"loaded {label} -> {path}")
    return 0


def uninstall(agents_dir: Path = AGENTS_DIR) -> int:
    domain = f"gui/{os.getuid()}"
    for label, path in plist_paths(agents_dir).items():
        if path.exists():
            if _launchctl("print", f"{domain}/{label}") == 0:
                _launchctl("bootout", domain, str(path))
            path.unlink()
            print(f"removed {label}")
        else:
            print(f"{label} is not installed")
    return 0


def status(agents_dir: Path = AGENTS_DIR) -> int:
    domain = f"gui/{os.getuid()}"
    missing = 0
    for label, path in plist_paths(agents_dir).items():
        loaded = _launchctl("print", f"{domain}/{label}") == 0
        print(f"{label}: {'loaded' if loaded else 'not loaded'} ({path})")
        missing += 0 if loaded else 1
    return 0 if missing == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("install", "uninstall", "status"))
    parser.add_argument("--python", default=sys.executable, help="interpreter for the agents (default: this one)")
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=6,
        help="how often the backup job checks for a stale archive (default 6)",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24,
        help="only archive when the newest one is older than this (0 = every run, default 24)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help="archives to keep (default: BACKUP_KEEP from .env)",
    )
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        print("launchd is macOS-only; use cron/systemd on Linux (see DEPLOYMENT.md)")
        return 2
    if args.command == "install":
        if not Path(args.python).exists():
            print(f"interpreter not found: {args.python}")
            return 1
        return install(args.python, args.interval_hours, args.keep, args.max_age_hours)
    if args.command == "uninstall":
        return uninstall()
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
