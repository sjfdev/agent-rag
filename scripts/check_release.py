"""Check that a release is consistent before tagging.

Verifies the three places a version lives:

  1. `src/version.py` -> `__version__` (what `/info` and metrics report)
  2. `CHANGELOG.md`    -> a `## [x.y.z]` heading for that version
  3. the git tag       -> `vX.Y.Z`, either on HEAD or (with --tag) checked

Also runs the practical release gates so a tag is never pushed from a broken
tree:

    python scripts/check_release.py            # local consistency + lint + tests
    python scripts/check_release.py --tag v1.0.0
    python scripts/check_release.py --no-tests # skip the slow gates

Exit code is non-zero on any failure, so it can gate CI or a manual release.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    line = f"{'PASS' if ok else 'FAIL'}  {name}"
    if detail:
        line += f"  {detail}"
    print(line)
    return ok


def read_version() -> str:
    from src.version import __version__

    return __version__


def changelog_has(version: str) -> bool:
    path = ROOT / "CHANGELOG.md"
    if not path.is_file():
        return False
    pattern = re.compile(rf"^##\s*\[{re.escape(version)}\]", re.MULTILINE)
    return bool(pattern.search(path.read_text(encoding="utf-8")))


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()


def run(cmd: list) -> bool:
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="tag to verify, e.g. v1.0.0")
    parser.add_argument("--no-tests", action="store_true", help="skip lint + pytest")
    args = parser.parse_args()

    version = read_version()
    check("version is semver", bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), version)
    check("CHANGELOG.md has this version", changelog_has(version), f"[{version}]")

    # A dirty tree means the tag would not describe the code that is committed.
    dirty = git("status", "--porcelain")
    check("working tree is clean", not dirty, dirty.splitlines()[0] if dirty else "")

    # The tag (if given or already present on HEAD) must match __version__.
    tag = args.tag
    if tag:
        check("tag matches version", tag.lstrip("v") == version, f"{tag} vs {version}")
    else:
        on_head = git("tag", "--points-at", "HEAD").split()
        if on_head:
            check(
                "HEAD tag matches version",
                any(t.lstrip("v") == version for t in on_head),
                " ".join(on_head),
            )
        else:
            print(f"NOTE  HEAD is untagged; expected tag v{version}")

    # Optional cross-check of the image tag used by docker-compose.prod.yml.
    compose = ROOT / "docker-compose.prod.yml"
    if compose.is_file():
        text = compose.read_text(encoding="utf-8")
        check(
            "compose uses APP_TAG for the image",
            "agent-rag:${APP_TAG" in text,
            "set APP_TAG=v" + version + " when building the release image",
        )

    if not args.no_tests:
        check("ruff", run([sys.executable, "-m", "ruff", "check",
                           "src", "tests", "main.py", "run_ingestion.py", "scripts"]))
        check("pytest", run([sys.executable, "-m", "pytest", "-q"]))

    print(f"\nRESULT: {'OK' if not FAIL else 'FAILED'} - {len(PASS)} passed, {len(FAIL)} failed")
    if not FAIL:
        print(f"\nNext: git tag -a v{version} -m \"release {version}\" && git push origin v{version}")
        print(f"      docker build -t agent-rag:v{version} .  (or APP_TAG=v{version} compose build)")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
