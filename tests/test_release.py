"""Version/changelog/tag consistency (scripts/check_release.py)."""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_release.py"


@pytest.fixture()
def release(monkeypatch):
    spec = importlib.util.spec_from_file_location("check_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    yield module
    module.PASS.clear()
    module.FAIL.clear()


def test_version_is_semver():
    from src.version import __version__

    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_changelog_records_the_running_version(release):
    from src.version import __version__

    assert release.changelog_has(__version__)


def test_changelog_missing_version_is_detected(release):
    assert release.changelog_has("99.99.99") is False


def test_changelog_links_and_heading(release):
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    from src.version import __version__

    assert f"## [{__version__}]" in text
    assert f"[{__version__}]: https://github.com/sjfdev/agent-rag/releases/tag/v{__version__}" in text


def test_compose_uses_app_tag():
    text = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "agent-rag:${APP_TAG" in text


def test_release_check_passes_without_git_gates(release, monkeypatch, capsys):
    # The working tree is dirty while developing, so stub that one gate out.
    monkeypatch.setattr(release, "git", lambda *args: "")
    monkeypatch.setattr("sys.argv", ["check_release.py", "--no-tests"])
    assert release.main() == 0
    output = capsys.readouterr().out
    assert "CHANGELOG.md has this version" in output
    assert "RESULT: OK" in output


def test_release_check_fails_on_version_mismatch(release, monkeypatch, capsys):
    monkeypatch.setattr(release, "git", lambda *args: "")
    monkeypatch.setattr("sys.argv", ["check_release.py", "--no-tests", "--tag", "v9.9.9"])
    assert release.main() == 1
    output = capsys.readouterr().out
    assert "FAIL  tag matches version" in output
    assert "RESULT: FAILED" in output


def test_release_check_fails_on_dirty_tree(release, monkeypatch, capsys):
    monkeypatch.setattr(release, "git", lambda *args: " M main.py")
    monkeypatch.setattr("sys.argv", ["check_release.py", "--no-tests"])
    assert release.main() == 1
    assert "FAIL  working tree is clean" in capsys.readouterr().out
