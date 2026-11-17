"""Monthly usage and value report for one deployment.

    python scripts/usage_report.py                        # previous month
    python scripts/usage_report.py --month 2026-10 --format markdown
    python scripts/usage_report.py --since 2026-10-01 --until 2026-10-31
    python scripts/usage_report.py --month 2026-10 --minutes-per-question 3 \\
        --hourly-rate 80 --out reports/acme-2026-10.md

Reads ``SESSIONS_DB_PATH`` (SQLite), ``AUDIT_LOG_PATH`` (JSON lines) and the
installed license, all offline. Output formats: text (terminal), markdown (for
a renewal/QBR document), csv (for a spreadsheet) and json (for automation).

The report contains aggregates only -- no questions, answers or user ids -- so
it can be shared with a customer's procurement team.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import license as license_module  # noqa: E402
from src.config import settings  # noqa: E402
from src.usage import (  # noqa: E402
    collect,
    render_csv,
    render_json,
    render_markdown,
    render_text,
    resolve_period,
)

FORMATS = ("text", "markdown", "md", "csv", "json")


def _as_markdown(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", help="calendar month, e.g. 2026-10 (default: previous month)")
    parser.add_argument("--since", help="start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--until", help="end date YYYY-MM-DD (inclusive)")
    parser.add_argument("--tz", default="Asia/Shanghai", help="time zone for day boundaries")
    parser.add_argument("--db", default=settings.SESSIONS_DB_PATH, help="session SQLite path")
    parser.add_argument("--audit", default=settings.AUDIT_LOG_PATH, help="audit JSON-lines path")
    parser.add_argument(
        "--format",
        default=None,
        choices=FORMATS,
        help="output format (inferred from --out suffix when omitted)",
    )
    parser.add_argument("--out", help="write to this file instead of stdout")
    parser.add_argument(
        "--minutes-per-question",
        type=float,
        default=0.0,
        help="optional value estimate: minutes saved per question",
    )
    parser.add_argument(
        "--hourly-rate",
        type=float,
        default=0.0,
        help="optional value estimate: currency per hour",
    )
    args = parser.parse_args()

    try:
        start, end = resolve_period(args.month, args.since, args.until, args.tz)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = collect(
        args.db,
        start,
        end,
        tz=args.tz,
        audit_path=args.audit,
        license_state=license_module.describe() if settings.LICENSE_ENABLED else None,
        minutes_per_question=args.minutes_per_question,
        hourly_rate=args.hourly_rate,
    )
    if report.get("error"):
        print(f"error: {report['error']}", file=sys.stderr)
        return 2

    fmt = args.format
    if not fmt and args.out:
        fmt = {
            ".md": "markdown",
            ".markdown": "markdown",
            ".csv": "csv",
            ".json": "json",
        }.get(Path(args.out).suffix.lower(), "text")
    fmt = "markdown" if fmt in (None, "md") else fmt

    if fmt == "markdown":
        text = render_markdown(report)
    elif fmt == "csv":
        text = render_csv(report)
    elif fmt == "json":
        text = render_json(report)
    else:
        text = render_text(report)

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_as_markdown(text), encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
