"""Usage and value metrics for customer reports.

The numbers come from local state only: the SQLite session store written by
``src/sessions.py``, the JSON-lines admin audit log and the verified license.
Nothing here calls the API or the LLM, so a monthly report can be produced on
the deployment host while the service is running.

The report is deliberately about *counts*, not conversation content: it never
includes questions, answers or user ids. That makes it safe to hand to a
customer's finance/procurement team and to attach to a renewal quote.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Asia/Shanghai"

_TOTALS_SQL = """
SELECT
    (SELECT COUNT(*) FROM sessions s
       WHERE s.created_at >= ? AND s.created_at < ?) AS new_sessions,
    (SELECT COUNT(DISTINCT s.user_id) FROM sessions s
       JOIN messages m ON m.session_id = s.id
       WHERE m.created_at >= ? AND m.created_at < ?) AS active_users,
    (SELECT COUNT(*) FROM messages m
       WHERE m.created_at >= ? AND m.created_at < ? AND m.role = 'user') AS questions,
    (SELECT COUNT(*) FROM messages m
       WHERE m.created_at >= ? AND m.created_at < ? AND m.role = 'assistant') AS answers,
    (SELECT COUNT(*) FROM messages m
       WHERE m.created_at >= ? AND m.created_at < ?
         AND m.role = 'assistant' AND m.degraded = 1) AS degraded_answers,
    (SELECT COUNT(*) FROM messages m
       WHERE m.created_at >= ? AND m.created_at < ?
         AND m.role = 'assistant'
         AND m.sources IS NOT NULL AND m.sources NOT IN ('', '[]')) AS cited_answers
"""

_ACTIVE_SESSIONS_SQL = """
SELECT COUNT(DISTINCT m.session_id) AS active_sessions
FROM messages m
WHERE m.created_at >= ? AND m.created_at < ?
"""

_MULTI_TURN_SQL = """
SELECT COUNT(*) AS multi_turn_sessions FROM (
    SELECT m.session_id
    FROM messages m
    WHERE m.created_at >= ? AND m.created_at < ? AND m.role = 'user'
    GROUP BY m.session_id
    HAVING COUNT(*) >= 2
)
"""

_FEEDBACK_SQL = """
SELECT
    COALESCE(SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END), 0) AS up,
    COALESCE(SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END), 0) AS down
FROM feedback
WHERE created_at >= ? AND created_at < ?
"""

_DEPTS_SQL = """
SELECT
    COALESCE(NULLIF(s.dept, ''), '(未标注)') AS dept,
    COUNT(*) AS questions,
    COUNT(DISTINCT s.user_id) AS users,
    COUNT(DISTINCT m.session_id) AS sessions
FROM messages m
JOIN sessions s ON s.id = m.session_id
WHERE m.created_at >= ? AND m.created_at < ? AND m.role = 'user'
GROUP BY COALESCE(NULLIF(s.dept, ''), '(未标注)')
ORDER BY questions DESC, dept ASC
"""


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone: {name}") from exc


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def month_bounds(month: str, tz: str = DEFAULT_TZ) -> Tuple[datetime, datetime]:
    """Return [start, end) for ``YYYY-MM`` in ``tz``."""
    zone = _zone(tz)
    try:
        first = datetime.strptime(month, "%Y-%m").replace(tzinfo=zone)
    except ValueError as exc:
        raise ValueError("month must look like YYYY-MM") from exc
    if first.month == 12:
        following = first.replace(year=first.year + 1, month=1)
    else:
        following = first.replace(month=first.month + 1)
    return first, following


def resolve_period(
    month: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tz: str = DEFAULT_TZ,
) -> Tuple[datetime, datetime]:
    """Resolve CLI-style period arguments into an aware [start, end) range.

    With no argument the previous calendar month is used, which is what a
    monthly billing/QBR cycle expects. ``--until`` is inclusive for humans.
    """
    zone = _zone(tz)
    if month and (since or until):
        raise ValueError("use either --month or --since/--until, not both")
    if month:
        return month_bounds(month, tz)
    if since or until:
        today = datetime.now(zone).date()
        try:
            start_date = date.fromisoformat(since) if since else today.replace(day=1)
            end_date = (
                date.fromisoformat(until) + timedelta(days=1)
                if until
                else today + timedelta(days=1)
            )
        except ValueError as exc:
            raise ValueError("dates must look like YYYY-MM-DD") from exc
        if start_date >= end_date:
            raise ValueError("--since must be before --until")
        return (
            datetime.combine(start_date, time.min, tzinfo=zone),
            datetime.combine(end_date, time.min, tzinfo=zone),
        )
    first_this = datetime.now(zone).date().replace(day=1)
    previous = first_this - timedelta(days=1)
    return month_bounds(previous.strftime("%Y-%m"), tz)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if not denominator:
        return None
    return round(numerator / denominator, 3)


def _connect(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _audit_counts(path: Optional[str], start_iso: str, end_iso: str) -> Dict[str, int]:
    """Count audit actions inside the period; never raises."""
    if not path:
        return {}
    file = Path(path).expanduser()
    if not file.is_file():
        return {}
    counts: Dict[str, int] = {}
    try:
        with file.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = record.get("ts")
                if not isinstance(stamp, str) or not (start_iso <= stamp < end_iso):
                    continue
                action = str(record.get("action") or "unknown")
                counts[action] = counts.get(action, 0) + 1
    except OSError:
        return {}
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _license_block(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not state:
        return None
    keys = (
        "enabled",
        "state",
        "trial",
        "edition",
        "licensee",
        "expires_at",
        "days_left",
        "max_depts",
        "max_docs",
        "features",
    )
    return {key: state.get(key) for key in keys}


def _empty_totals() -> Dict[str, Any]:
    return {
        "sessions": 0,
        "active_sessions": 0,
        "active_users": 0,
        "questions": 0,
        "answers": 0,
        "degraded_answers": 0,
        "cited_answers": 0,
        "citation_rate": None,
        "feedback_up": 0,
        "feedback_down": 0,
        "satisfaction": None,
        "questions_per_session": 0.0,
        "multi_turn_sessions": 0,
    }


def _period_block(start: datetime, end: datetime, tz: str) -> Dict[str, str]:
    return {
        "label": start.strftime("%Y-%m"),
        "start": start.strftime("%Y-%m-%d"),
        "end": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "timezone": tz,
    }


def collect(
    db_path: str,
    start: datetime,
    end: datetime,
    *,
    tz: str = DEFAULT_TZ,
    audit_path: Optional[str] = None,
    license_state: Optional[Dict[str, Any]] = None,
    minutes_per_question: float = 0.0,
    hourly_rate: float = 0.0,
) -> Dict[str, Any]:
    """Build one report dict from local state.

    A missing database or an older schema is not fatal: the report is returned
    with zeroed totals and a warning, so a scheduled monthly job still produces
    an artefact on a deployment that has not served any chats yet.
    """
    db = Path(db_path).expanduser()
    start_iso, end_iso = _utc_iso(start), _utc_iso(end)
    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": _period_block(start, end, tz),
        "source": {
            "db": str(db),
            "db_exists": db.is_file(),
            "audit": str(Path(audit_path).expanduser()) if audit_path else None,
        },
        "license": _license_block(license_state),
        "totals": _empty_totals(),
        "departments": [],
        "audit": {},
        "warnings": [],
        "estimate": None,
    }
    if not db.is_file():
        report["warnings"].append(f"session database not found: {db}")
        report["audit"] = _audit_counts(audit_path, start_iso, end_iso)
        return report

    try:
        connection = _connect(db)
    except sqlite3.Error as exc:
        report["error"] = f"cannot open session database: {exc}"
        return report

    try:
        if not _has_table(connection, "sessions") or not _has_table(connection, "messages"):
            report["warnings"].append("session database has no sessions/messages tables")
            return report
        totals_row = connection.execute(
            _TOTALS_SQL,
            (start_iso, end_iso) * 6,
        ).fetchone()
        active_row = connection.execute(_ACTIVE_SESSIONS_SQL, (start_iso, end_iso)).fetchone()
        multi_row = connection.execute(_MULTI_TURN_SQL, (start_iso, end_iso)).fetchone()
        feedback_row = (
            connection.execute(_FEEDBACK_SQL, (start_iso, end_iso)).fetchone()
            if _has_table(connection, "feedback")
            else None
        )
        dept_rows = connection.execute(_DEPTS_SQL, (start_iso, end_iso)).fetchall()
    except sqlite3.Error as exc:
        report["error"] = f"cannot read session database: {exc}"
        return report
    finally:
        connection.close()

    totals = _empty_totals()
    totals.update(
        sessions=int(totals_row["new_sessions"] or 0),
        active_users=int(totals_row["active_users"] or 0),
        questions=int(totals_row["questions"] or 0),
        answers=int(totals_row["answers"] or 0),
        degraded_answers=int(totals_row["degraded_answers"] or 0),
        cited_answers=int(totals_row["cited_answers"] or 0),
        active_sessions=int(active_row["active_sessions"] or 0),
        multi_turn_sessions=int(multi_row["multi_turn_sessions"] or 0),
    )
    totals["citation_rate"] = _rate(totals["cited_answers"], totals["answers"])
    if totals["active_sessions"]:
        totals["questions_per_session"] = round(
            totals["questions"] / totals["active_sessions"], 1
        )
    up = int(feedback_row["up"] or 0) if feedback_row else 0
    down = int(feedback_row["down"] or 0) if feedback_row else 0
    totals.update(
        feedback_up=up,
        feedback_down=down,
        satisfaction=_rate(up, up + down),
    )

    report["totals"] = totals
    report["departments"] = [
        {
            "dept": row["dept"],
            "questions": int(row["questions"] or 0),
            "users": int(row["users"] or 0),
            "sessions": int(row["sessions"] or 0),
        }
        for row in dept_rows
    ]
    report["audit"] = _audit_counts(audit_path, start_iso, end_iso)

    if minutes_per_question > 0:
        hours = totals["questions"] * float(minutes_per_question) / 60.0
        report["estimate"] = {
            "minutes_per_question": float(minutes_per_question),
            "hours_saved": round(hours, 1),
            "hourly_rate": float(hourly_rate),
            "amount": round(hours * float(hourly_rate), 2) if hourly_rate > 0 else None,
        }
    return report


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return f"{text}{suffix}"


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _license_lines(report: Dict[str, Any]) -> List[str]:
    state = report.get("license")
    if not state:
        return ["许可：未启用（LICENSE_ENABLED=false）"]
    label = "试用" if state.get("trial") else "正式"
    lines = [
        f"客户：{_fmt(state.get('licensee'))}（{_fmt(state.get('edition'))} / {label}）",
        f"许可状态：{_fmt(state.get('state'))}，到期 {_fmt(state.get('expires_at'))}"
        f"（剩余 {_fmt(state.get('days_left'))} 天）",
    ]
    limits = []
    if state.get("max_depts"):
        limits.append(f"{state['max_depts']} 个部门")
    if state.get("max_docs"):
        limits.append(f"{state['max_docs']} 份文档")
    if limits:
        lines.append("许可上限：" + "、".join(limits))
    return lines


def render_text(report: Dict[str, Any]) -> str:
    period = report["period"]
    totals = report["totals"]
    lines = [
        "Agent RAG 用量与价值报告",
        "=" * 32,
        *_license_lines(report),
        f"周期：{period['start']} ~ {period['end']}（{period['timezone']}）",
        "",
        f"会话            {totals['sessions']} 个（活跃 {totals['active_sessions']}）",
        f"活跃用户        {totals['active_users']} 人",
        f"提问 / 回答     {totals['questions']} / {totals['answers']} 条",
        f"引用覆盖        {_pct(totals['citation_rate'])}"
        f"（{totals['cited_answers']} 条回答带来源）",
        f"降级回答        {totals['degraded_answers']} 条",
        f"多轮会话        {totals['multi_turn_sessions']} 个",
        f"满意度          {_pct(totals['satisfaction'])}"
        f"（赞 {totals['feedback_up']} / 踩 {totals['feedback_down']}）",
    ]
    if totals["active_sessions"]:
        lines.append(f"户均提问        {totals['questions_per_session']} 条/会话")
    if report["departments"]:
        lines.extend(["", "部门分布："])
        for row in report["departments"]:
            lines.append(
                f"  {row['dept']:<14} 提问 {row['questions']:>4}  "
                f"用户 {row['users']:>3}  会话 {row['sessions']:>3}"
            )
    if report["audit"]:
        lines.extend(["", "管理操作："])
        for action, count in report["audit"].items():
            lines.append(f"  {action:<20} {count}")
    estimate = report.get("estimate")
    if estimate:
        lines.extend(
            [
                "",
                "价值估算（按参数，仅作参考）：",
                f"  节省工时 {estimate['hours_saved']} 小时"
                f"（每条提问 {estimate['minutes_per_question']} 分钟）",
            ]
        )
        if estimate["amount"] is not None:
            lines.append(f"  折算金额 {estimate['amount']:.2f} 元（时薪 {estimate['hourly_rate']} 元）")
    for warning in report["warnings"]:
        lines.append(f"注意：{warning}")
    return "\n".join(lines)


def render_markdown(report: Dict[str, Any]) -> str:
    period = report["period"]
    totals = report["totals"]
    state = report.get("license")
    lines = [
        "# Agent RAG 用量与价值报告",
        "",
        f"**客户**：{_fmt(state.get('licensee')) if state else '未启用许可'}"
        f"（{_fmt(state.get('edition')) if state else '—'}）",
        f"**周期**：{period['start']} ~ {period['end']}（{period['timezone']}）",
        f"**生成时间**：{report['generated_at']}",
        "",
        "## 概览",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 会话数（期内新建） | {totals['sessions']} |",
        f"| 活跃会话 | {totals['active_sessions']} |",
        f"| 活跃用户 | {totals['active_users']} |",
        f"| 提问数 | {totals['questions']} |",
        f"| 回答数 | {totals['answers']} |",
        f"| 引用覆盖 | {_pct(totals['citation_rate'])}"
        f"（{totals['cited_answers']} 条带来源） |",
        f"| 降级回答 | {totals['degraded_answers']} |",
        f"| 多轮会话 | {totals['multi_turn_sessions']} |",
        f"| 满意度 | {_pct(totals['satisfaction'])}"
        f"（赞 {totals['feedback_up']} / 踩 {totals['feedback_down']}） |",
        f"| 户均提问 | {totals['questions_per_session']} 条/活跃会话 |",
        "",
    ]
    if report["departments"]:
        lines.extend(
            [
                "## 部门分布",
                "",
                "| 部门 | 提问 | 活跃用户 | 会话 |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for row in report["departments"]:
            lines.append(
                f"| {row['dept']} | {row['questions']} | {row['users']} | {row['sessions']} |"
            )
        lines.append("")
    if state:
        lines.extend(
            [
                "## 许可",
                "",
                f"- 版本：{_fmt(state.get('edition'))}"
                f"{'（试用）' if state.get('trial') else ''}",
                f"- 状态：{_fmt(state.get('state'))}",
                f"- 到期：{_fmt(state.get('expires_at'))}"
                f"（剩余 {_fmt(state.get('days_left'))} 天）",
                f"- 上限：{_fmt(state.get('max_depts'))} 个部门 /"
                f" {_fmt(state.get('max_docs'))} 份文档",
                "",
            ]
        )
    if report["audit"]:
        lines.extend(["## 管理操作", "", "| 操作 | 次数 |", "| --- | ---: |"])
        for action, count in report["audit"].items():
            lines.append(f"| `{action}` | {count} |")
        lines.append("")
    estimate = report.get("estimate")
    if estimate:
        lines.extend(
            [
                "## 价值估算",
                "",
                f"- 每条提问按 {estimate['minutes_per_question']} 分钟计，"
                f"期内节省约 **{estimate['hours_saved']} 小时**。",
            ]
        )
        if estimate["amount"] is not None:
            lines.append(
                f"- 按 {estimate['hourly_rate']} 元/小时折算，约 **{estimate['amount']:.2f} 元**。"
            )
        lines.append("")
    if report["warnings"]:
        lines.extend(["## 注意", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
        lines.append("")
    lines.extend(
        [
            "---",
            "",
            "本报告只包含计数与聚合指标，不含原始对话内容或用户标识。",
        ]
    )
    return "\n".join(lines)


def render_csv(report: Dict[str, Any]) -> str:
    period = report["period"]
    totals = report["totals"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["section", "metric", "value"])
    writer.writerow(["period", "start", period["start"]])
    writer.writerow(["period", "end", period["end"]])
    writer.writerow(["period", "timezone", period["timezone"]])
    for key, value in totals.items():
        writer.writerow(["totals", key, "" if value is None else value])
    for row in report["departments"]:
        writer.writerow(["department", f"{row['dept']}.questions", row["questions"]])
        writer.writerow(["department", f"{row['dept']}.users", row["users"]])
        writer.writerow(["department", f"{row['dept']}.sessions", row["sessions"]])
    for action, count in report["audit"].items():
        writer.writerow(["audit", action, count])
    state = report.get("license") or {}
    for key in ("licensee", "edition", "state", "expires_at", "days_left"):
        if state.get(key) is not None:
            writer.writerow(["license", key, state[key]])
    return buffer.getvalue()


def render_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
