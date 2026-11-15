"""Boot recovery, daily backup and the macOS launchd agents."""

import importlib.util
import plistlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def installer():
    return _load("install_launchd", "install_launchd.py")


@pytest.fixture()
def daily():
    return _load("daily_backup", "daily_backup.py")


@pytest.fixture()
def boot():
    return _load("boot_recover", "boot_recover.py")


def test_boot_agent_runs_at_login(installer):
    payload = installer.boot_payload("/usr/bin/python3", Path("/tmp/logs"))
    parsed = plistlib.loads(installer.render_plist(payload))
    assert parsed["Label"] == installer.BOOT_LABEL
    assert parsed["RunAtLoad"] is True
    assert parsed["ProgramArguments"][1].endswith("scripts/boot_recover.py")
    assert parsed["StandardOutPath"] == "/tmp/logs/boot-agent.log"
    assert parsed["EnvironmentVariables"]["PATH"].startswith("/usr/local/bin")


def test_backup_agent_runs_daily(installer):
    payload = installer.backup_payload("/usr/bin/python3", Path("/tmp/logs"), 3, 30, 14)
    parsed = plistlib.loads(installer.render_plist(payload))
    assert parsed["Label"] == installer.BACKUP_LABEL
    assert "RunAtLoad" not in parsed
    assert parsed["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    assert parsed["ProgramArguments"][-2:] == ["--keep", "14"]
    assert parsed["ProgramArguments"][1].endswith("scripts/daily_backup.py")


def test_plist_paths_live_in_launch_agents(installer):
    paths = installer.plist_paths(Path("/tmp/LaunchAgents"))
    assert paths[installer.BOOT_LABEL].name == "com.agent-rag.boot-recover.plist"
    assert paths[installer.BACKUP_LABEL].name == "com.agent-rag.daily-backup.plist"


def test_prune_keeps_newest_archives(daily, tmp_path):
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        archive = tmp_path / f"agent-rag-backup-{stamp}.tar.gz"
        archive.write_bytes(b"x")
        archive.with_name(archive.name + ".sha256").write_text("hash")
    removed = daily.prune_archives(tmp_path, keep=1)
    assert sorted(removed) == [
        "agent-rag-backup-20260101-000000.tar.gz",
        "agent-rag-backup-20260101-000000.tar.gz.sha256",
        "agent-rag-backup-20260102-000000.tar.gz",
        "agent-rag-backup-20260102-000000.tar.gz.sha256",
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "agent-rag-backup-20260103-000000.tar.gz",
        "agent-rag-backup-20260103-000000.tar.gz.sha256",
    ]


def test_offsite_copy_is_verified(daily, tmp_path):
    archive = tmp_path / "agent-rag-backup-20260101-000000.tar.gz"
    archive.write_bytes(b"payload")
    archive.with_name(archive.name + ".sha256").write_text("hash")
    target = daily.copy_offsite(archive, str(tmp_path / "offsite"))
    assert target.read_bytes() == b"payload"
    assert (target.parent / (archive.name + ".sha256")).exists()


def test_daily_backup_restarts_stack_after_a_failure(daily, tmp_path):
    class FakeBackup:
        @staticmethod
        def create_backup(dest=None):
            raise RuntimeError("disk full")

    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        return 0

    rc = daily.run_daily(
        dest=tmp_path,
        stop=True,
        runner=runner,
        capture=lambda cmd: "milvus-standalone\n",
        backup_module=FakeBackup,
    )
    assert rc == 1
    assert ["docker", "compose", "-f", str(daily.PROD_COMPOSE), "up", "-d"] in calls
    assert ["docker", "start", "milvus-standalone"] in calls


def test_daily_backup_dry_run_touches_nothing(daily, tmp_path):
    def runner(cmd):
        raise AssertionError(f"unexpected command: {cmd}")

    assert daily.run_daily(dest=tmp_path, dry_run=True, runner=runner) == 0


def test_boot_recover_starts_colima_then_waits_for_the_app(boot):
    state = {"docker": 1}
    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "info"]:
            return state["docker"]
        if cmd[0] == "colima":
            state["docker"] = 0
        return 0

    rc = boot.recover(
        timeout=1, runner=runner, liveness=lambda: True, sleep=lambda _: None
    )
    assert rc == 0
    assert ["colima", "start"] in calls
    assert ["docker", "compose", "-f", str(boot.PROD_COMPOSE), "up", "-d"] in calls
    assert ["docker", "compose", "-f", str(boot.MONITOR_COMPOSE), "up", "-d"] in calls


def test_boot_recover_reports_a_dead_app(boot):
    def runner(cmd):
        return 0

    rc = boot.recover(
        timeout=1, runner=runner, liveness=lambda: False, sleep=lambda _: None
    )
    assert rc == 2


def test_boot_recover_survives_monitoring_being_down(boot):
    def runner(cmd):
        if cmd[:2] == ["docker", "compose"] and str(boot.MONITOR_COMPOSE) in cmd:
            return 1
        return 0

    rc = boot.recover(
        timeout=1, runner=runner, liveness=lambda: True, sleep=lambda _: None
    )
    assert rc == 0


def test_daily_backup_archives_the_prod_vector_store(daily, tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(daily, "prod_milvus_present", lambda: True)
    monkeypatch.setattr(settings, "VECTOR_STORE_TYPE", "faiss", raising=False)
    seen = {}

    class FakeBackup:
        @staticmethod
        def create_backup(dest=None):
            seen["store"] = settings.VECTOR_STORE_TYPE
            path = tmp_path / "agent-rag-backup-20260101-000000.tar.gz"
            path.write_bytes(b"x")
            return path

    rc = daily.run_daily(
        dest=tmp_path,
        runner=lambda cmd: 0,
        capture=lambda cmd: "",
        backup_module=FakeBackup,
    )
    assert rc == 0
    assert seen["store"] == "milvus"


def test_silence_payload_covers_the_window(daily):
    now = datetime(2026, 1, 1, 3, 30, tzinfo=timezone.utc)
    payload = daily.silence_payload(15, now)
    assert payload["startsAt"] == "2026-01-01T03:30:00Z"
    assert payload["endsAt"] == "2026-01-01T03:45:00Z"
    assert payload["createdBy"] == "daily-backup"
    assert payload["matchers"][0]["value"] == "AgentRagHealthDown"


def test_daily_backup_silences_the_readiness_alert(daily, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        daily, "create_silence", lambda minutes: seen.setdefault("minutes", minutes) and True
    )

    class FakeBackup:
        @staticmethod
        def create_backup(dest=None):
            path = tmp_path / "agent-rag-backup-20260101-000000.tar.gz"
            path.write_bytes(b"x")
            return path

    rc = daily.run_daily(
        dest=tmp_path,
        runner=lambda cmd: 0,
        capture=lambda cmd: "",
        backup_module=FakeBackup,
        silence_minutes=15,
    )
    assert rc == 0
    assert seen["minutes"] == 15
