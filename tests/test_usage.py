"""Offline usage/value report: period bounds, SQL aggregation and renderers.

离线用量/价值月报：周期边界、SQL 聚合与三种输出格式。
"""

import csv
import importlib.util
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sessions import SessionStore
from src.usage import collect, render_csv, render_json, render_markdown, render_text

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "usage_report.py"


def _seed(path: Path) -> Path:
    """Create a session DB with fixed October 2026 rows."""
    store = SessionStore(str(path))
    store.create_session("warmup", "Warmup")
    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM sessions")
        sessions = [
            ("s1", "u1", "Tech", "2026-10-05T02:00:00+00:00"),
            ("s2", "u2", "Finance", "2026-10-06T03:00:00+00:00"),
            ("s3", "u1", "Tech", "2026-09-30T10:00:00+00:00"),
        ]
        for session_id, user_id, dept, stamp in sessions:
            connection.execute(
                "INSERT INTO sessions (id, user_id, dept, title, summary, "
                "summarized_through, created_at, updated_at) "
                "VALUES (?, ?, ?, 'title', '', 0, ?, ?)",
                (session_id, user_id, dept, stamp, stamp),
            )
        messages = [
            ("s1", 1, "user", "q1", "[]", 0, "2026-10-05T02:01:00+00:00"),
            (
                "s1",
                2,
                "assistant",
                "a1",
                '[{"source": "policy.pdf", "page": 1}]',
                0,
                "2026-10-05T02:02:00+00:00",
            ),
            ("s1", 3, "user", "q2", "[]", 0, "2026-10-05T03:00:00+00:00"),
            ("s1", 4, "assistant", "a2", "[]", 0, "2026-10-05T03:01:00+00:00"),
            ("s1", 5, "user", "q3", "[]", 0, "2026-10-06T01:00:00+00:00"),
            ("s1", 6, "assistant", "a3", "[]", 1, "2026-10-06T01:01:00+00:00"),
            ("s2", 1, "user", "q4", "[]", 0, "2026-10-06T03:01:00+00:00"),
            (
                "s2",
                2,
                "assistant",
                "a4",
                '[{"source": "finance.xlsx", "sheet": "Q3"}]',
                0,
                "2026-10-06T03:02:00+00:00",
            ),
            ("s3", 1, "user", "old", "[]", 0, "2026-09-30T10:01:00+00:00"),
            ("s3", 2, "assistant", "old-a", "[]", 0, "2026-09-30T10:02:00+00:00"),
        ]
        connection.executemany(
            "INSERT INTO messages (session_id, seq, role, content, sources, "
            "degraded, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            messages,
        )
        connection.executemany(
            "INSERT INTO feedback (session_id, seq, user_id, rating, comment, "
            "created_at) VALUES (?, ?, ?, ?, '', ?)",
            [
                ("s1", 2, "u1", 1, "2026-10-06T02:00:00+00:00"),
                ("s2", 2, "u2", -1, "2026-10-07T02:00:00+00:00"),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture()
def seeded(tmp_path):
    db = _seed(tmp_path / "sessions.db")
    audit = tmp_path / "audit.log"
    audit.write_text(
        "\n".join(
            [
                json.dumps(
                    {"ts": "2026-10-05T02:00:00+00:00", "action": "file.upload", "ok": True}
                ),
                json.dumps(
                    {"ts": "2026-10-05T02:00:01+00:00", "action": "ingest.start", "ok": True}
                ),
                json.dumps(
                    {"ts": "2026-09-01T00:00:00+00:00", "action": "file.upload", "ok": True}
                ),
                "not json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return db, audit


def _october():
    return (
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 11, 1, tzinfo=timezone.utc),
    )


def test_month_bounds_rolls_over_year():
    from src.usage import month_bounds

    start, end = month_bounds("2026-12", "UTC")
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_resolve_period_defaults_to_previous_month(monkeypatch):
    from src import usage

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 17, 12, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(usage, "datetime", Frozen)
    start, end = usage.resolve_period(tz="UTC")
    assert start.strftime("%Y-%m-%d") == "2026-10-01"
    assert end.strftime("%Y-%m-%d") == "2026-11-01"


def test_collect_counts_october(seeded):
    db, audit = seeded
    start, end = _october()
    report = collect(
        str(db),
        start,
        end,
        tz="UTC",
        audit_path=str(audit),
        license_state={
            "enabled": True,
            "state": "valid",
            "trial": False,
            "edition": "enterprise",
            "licensee": "ACME",
            "expires_at": "2026-12-31",
            "days_left": 45,
            "max_depts": 5,
            "max_docs": 100000,
            "features": ["sso"],
        },
        minutes_per_question=3,
        hourly_rate=80,
    )
    totals = report["totals"]
    assert totals["sessions"] == 2
    assert totals["active_sessions"] == 2
    assert totals["active_users"] == 2
    assert totals["questions"] == 4
    assert totals["answers"] == 4
    assert totals["degraded_answers"] == 1
    assert totals["cited_answers"] == 2
    assert totals["citation_rate"] == 0.5
    assert totals["feedback_up"] == 1 and totals["feedback_down"] == 1
    assert totals["satisfaction"] == 0.5
    assert totals["multi_turn_sessions"] == 1
    assert totals["questions_per_session"] == 2.0
    assert report["departments"] == [
        {"dept": "Tech", "questions": 3, "users": 1, "sessions": 1},
        {"dept": "Finance", "questions": 1, "users": 1, "sessions": 1},
    ]
    assert report["audit"] == {"file.upload": 1, "ingest.start": 1}
    assert report["license"]["licensee"] == "ACME"
    assert report["estimate"]["hours_saved"] == 0.2
    assert report["estimate"]["amount"] == 16.0


def test_collect_missing_db_warns_and_zeroes(tmp_path):
    start, end = _october()
    report = collect(str(tmp_path / "missing.db"), start, end, tz="UTC")
    assert report["source"]["db_exists"] is False
    assert report["totals"]["questions"] == 0
    assert report["warnings"]


def test_collect_reports_schema_error(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    start, end = _october()
    report = collect(str(db), start, end, tz="UTC")
    assert "no sessions/messages tables" in report["warnings"][0]


def test_renderers_include_the_customer(seeded):
    db, audit = seeded
    start, end = _october()
    report = collect(
        str(db),
        start,
        end,
        tz="UTC",
        audit_path=str(audit),
        license_state={"enabled": True, "state": "valid", "licensee": "ACME", "edition": "enterprise"},
        minutes_per_question=3,
        hourly_rate=80,
    )
    markdown = render_markdown(report)
    assert "# Agent RAG 用量与价值报告" in markdown
    assert "ACME" in markdown
    assert "| Tech | 3 | 1 | 1 |" in markdown
    assert "**0.2 小时**" in markdown

    text = render_text(report)
    assert "提问 / 回答     4 / 4 条" in text
    assert "Tech" in text

    rows = list(csv.reader(io.StringIO(render_csv(report))))
    assert ["totals", "questions", "4"] in rows
    assert ["department", "Tech.questions", "3"] in rows

    payload = json.loads(render_json(report))
    assert payload["totals"]["satisfaction"] == 0.5


def test_cli_writes_markdown(tmp_path, seeded, monkeypatch):
    db, audit = seeded
    spec = importlib.util.spec_from_file_location("usage_report_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    out = tmp_path / "report.md"
    from src.config import settings

    monkeypatch.setattr(settings, "LICENSE_ENABLED", False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "usage_report.py",
            "--db",
            str(db),
            "--audit",
            str(audit),
            "--month",
            "2026-10",
            "--tz",
            "UTC",
            "--format",
            "markdown",
            "--out",
            str(out),
        ],
    )
    assert module.main() == 0
    body = out.read_text(encoding="utf-8")
    assert "2026-10-01 ~ 2026-10-31" in body
    assert "| 提问数 | 4 |" in body


def test_cli_rejects_conflicting_period(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("usage_report_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        "sys.argv",
        ["usage_report.py", "--month", "2026-10", "--since", "2026-10-01"],
    )
    assert module.main() == 2
    assert "either --month" in capsys.readouterr().err
