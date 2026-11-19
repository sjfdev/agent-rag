"""Boot recovery, daily backup and the macOS launchd agents."""

import importlib.util
import os
import plistlib
from datetime import datetime, timedelta, timezone
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


def test_backup_agent_checks_on_an_interval(installer):
    payload = installer.backup_payload("/usr/bin/python3", Path("/tmp/logs"), 6, 14, 24)
    parsed = plistlib.loads(installer.render_plist(payload))
    assert parsed["Label"] == installer.BACKUP_LABEL
    # An interval job is coalesced by launchd and runs on wake; a 03:30
    # calendar job never fired on a machine that sleeps at night (runs = 0).
    assert parsed["StartInterval"] == 6 * 3600
    assert "StartCalendarInterval" not in parsed
    assert parsed["RunAtLoad"] is True
    arguments = parsed["ProgramArguments"]
    assert arguments[1].endswith("scripts/daily_backup.py")
    assert arguments[2:4] == ["--max-age-hours", "24"]
    assert arguments[-2:] == ["--keep", "14"]


def test_backup_agent_defaults_to_six_hours_and_24h_staleness(installer):
    parsed = plistlib.loads(installer.render_plist(installer.backup_payload("/usr/bin/python3")))
    assert parsed["StartInterval"] == 21600
    assert parsed["ProgramArguments"][2:4] == ["--max-age-hours", "24"]


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


def _archive(dest, name="agent-rag-backup-20260101-000000.tar.gz"):
    path = dest / name
    path.write_bytes(b"x")
    return path


def test_backup_age_and_staleness(daily, tmp_path):
    now = datetime.now(timezone.utc)
    assert daily.newest_archive(tmp_path) is None
    assert daily.backup_age_hours(tmp_path, now) is None
    # Nothing to compare against: a backup is due, and 0 means "always".
    assert daily.is_stale(tmp_path, 24, now) is True
    assert daily.is_stale(tmp_path, 0, now) is True

    fresh = _archive(tmp_path)
    os.utime(fresh, (now.timestamp(), now.timestamp()))
    assert daily.backup_age_hours(tmp_path, now) == pytest.approx(0.0, abs=1e-3)
    assert daily.is_stale(tmp_path, 24, now) is False
    assert daily.is_stale(tmp_path, 0, now) is True

    # The newest archive wins, not the alphabetically last one.
    newer = _archive(tmp_path, "agent-rag-backup-20260102-000000.tar.gz")
    os.utime(newer, ((now + timedelta(hours=2)).timestamp(),) * 2)
    assert daily.newest_archive(tmp_path) == newer


def test_daily_backup_skips_when_the_archive_is_fresh(daily, tmp_path):
    fresh = _archive(tmp_path)
    now = datetime.now(timezone.utc)
    os.utime(fresh, (now.timestamp(), now.timestamp()))

    def runner(cmd):
        raise AssertionError(f"a fresh archive must not touch docker: {cmd}")

    assert daily.run_daily(dest=tmp_path, max_age_hours=24, runner=runner) == 0


def test_daily_backup_is_unconditional_without_max_age(daily, tmp_path):
    fresh = _archive(tmp_path)
    now = datetime.now(timezone.utc)
    os.utime(fresh, (now.timestamp(), now.timestamp()))
    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        return 0

    assert daily.run_daily(dest=tmp_path, stop=False, runner=runner) == 0
    assert calls, "max_age_hours=0 must still run the backup"


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
        timeout=1,
        runner=runner,
        probe=lambda cmd: "",
        liveness=lambda: True,
        sleep=lambda _: None,
    )
    assert rc == 0
    assert ["colima", "start"] in calls
    assert ["docker", "compose", "-f", str(boot.PROD_COMPOSE), "up", "-d"] in calls
    assert ["docker", "compose", "-f", str(boot.MONITOR_COMPOSE), "up", "-d"] in calls


def test_boot_recover_stops_a_leftover_dev_milvus(boot):
    """A second 5 GB Milvus in the same 8 GB VM gets both stacks OOM-killed."""
    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        return 0

    rc = boot.recover(
        timeout=1,
        runner=runner,
        probe=lambda cmd: "milvus-standalone\n" if cmd[0] == "docker" else "",
        liveness=lambda: True,
        sleep=lambda _: None,
    )
    assert rc == 0
    assert ["docker", "compose", "-f", str(boot.DEV_COMPOSE), "down", "--remove-orphans"] in calls


def test_boot_recover_leaves_the_dev_stack_alone_when_it_is_down(boot):
    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        return 0

    rc = boot.recover(
        timeout=1,
        runner=runner,
        probe=lambda cmd: "",
        liveness=lambda: True,
        sleep=lambda _: None,
    )
    assert rc == 0
    assert ["docker", "compose", "-f", str(boot.DEV_COMPOSE), "down", "--remove-orphans"] not in calls


def test_boot_recover_retries_a_stack_that_was_not_ready_yet(boot):
    """A hard VM stop leaves Milvus slower than compose's dependency wait."""
    calls = []
    state = {"attempts": 0}

    def runner(cmd):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "compose"] and str(boot.PROD_COMPOSE) in cmd:
            state["attempts"] += 1
            return 1 if state["attempts"] == 1 else 0
        return 0

    rc = boot.recover(
        timeout=1,
        runner=runner,
        probe=lambda cmd: "",
        liveness=lambda: True,
        sleep=lambda _: None,
        retry_delay=0,
    )
    assert rc == 0
    ups = [c for c in calls if str(boot.PROD_COMPOSE) in c and "up" in c]
    assert len(ups) == 2


def test_boot_recover_gives_up_after_the_last_retry(boot):
    def runner(cmd):
        if cmd[:2] == ["docker", "compose"] and str(boot.PROD_COMPOSE) in cmd:
            return 1
        return 0

    rc = boot.recover(
        timeout=1,
        runner=runner,
        probe=lambda cmd: "",
        liveness=lambda: True,
        sleep=lambda _: None,
        retry_delay=0,
    )
    assert rc == 1


def test_boot_recover_reports_a_dead_app(boot):
    def runner(cmd):
        return 0

    rc = boot.recover(
        timeout=1,
        runner=runner,
        probe=lambda cmd: "",
        liveness=lambda: False,
        sleep=lambda _: None,
    )
    assert rc == 2


def test_boot_recover_survives_monitoring_being_down(boot):
    def runner(cmd):
        if cmd[:2] == ["docker", "compose"] and str(boot.MONITOR_COMPOSE) in cmd:
            return 1
        return 0

    rc = boot.recover(
        timeout=1,
        runner=runner,
        probe=lambda cmd: "",
        liveness=lambda: True,
        sleep=lambda _: None,
    )
    assert rc == 0


def test_boot_recover_runs_the_backup_catch_up(boot):
    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        return 0

    rc = boot.recover(
        timeout=1,
        runner=runner,
        probe=lambda cmd: "",
        liveness=lambda: True,
        sleep=lambda _: None,
    )
    assert rc == 0
    catch_up = [c for c in calls if "daily_backup.py" in " ".join(c)]
    assert len(catch_up) == 1
    assert catch_up[0][-2:] == ["--max-age-hours", "24"]


def test_boot_recover_backup_failure_does_not_fail_the_boot(boot):
    def runner(cmd):
        return 1 if "daily_backup.py" in " ".join(cmd) else 0

    rc = boot.recover(
        timeout=1,
        runner=runner,
        probe=lambda cmd: "",
        liveness=lambda: True,
        sleep=lambda _: None,
    )
    assert rc == 0


def test_boot_recover_can_skip_the_catch_up(boot):
    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        return 0

    rc = boot.recover(
        timeout=1,
        runner=runner,
        probe=lambda cmd: "",
        liveness=lambda: True,
        sleep=lambda _: None,
        catch_up_backup=False,
    )
    assert rc == 0
    assert not [c for c in calls if "daily_backup.py" in " ".join(c)]


def test_daily_backup_refuses_when_another_run_holds_the_lock(daily, tmp_path):
    lock = daily.acquire_lock(tmp_path / "backup.lock")
    assert lock is not None
    # A second run (e.g. boot-recover's catch-up next to the interval agent)
    # must not stop/start the stack concurrently.
    assert daily.acquire_lock(tmp_path / "backup.lock") is None
    lock.close()
    assert daily.acquire_lock(tmp_path / "backup.lock") is not None


def test_main_skips_while_the_lock_is_held(daily, tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "LOCK_PATH", tmp_path / "backup.lock")
    monkeypatch.setattr(
        daily, "run_daily", lambda **kwargs: pytest.fail("a locked run must not back up")
    )
    lock = daily.acquire_lock(tmp_path / "backup.lock")
    assert lock is not None
    try:
        assert daily.main([]) == 0
    finally:
        lock.close()


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
