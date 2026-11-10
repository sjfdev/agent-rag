"""The chat UI's server-session request builder, exercised through Node.

`static/sessions.js` decides whether a chat request carries `session_id` (the
server owns the transcript) or the bounded local `history` fallback. That choice
is easy to regress silently, so it is asserted here as a pure function.

Skipped (not failed) when Node is unavailable, keeping the Python suite offline.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HISTORY = [
    {"role": "assistant", "content": "stale"},
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"},
    {"role": "assistant", "content": "a2"},
]


def _run(code, payload=None):
    """Run a snippet with `require(process.argv[1])` and parse its JSON output."""
    args = [NODE, "-e", code, str(SCRIPT)]
    if payload is not None:
        args.append(json.dumps(payload))
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _build(case):
    code = """
const api = require(process.argv[1]);
const body = api.buildChatBody(JSON.parse(process.argv[2]));
process.stdout.write(JSON.stringify(body));
"""
    return _run(code, case)


def test_session_id_wins_over_local_history():
    body = _build(
        {
            "sessionId": "sid",
            "history": HISTORY,
            "maxHistory": 20,
            "userId": "u1",
            "dept": "Tech",
            "message": "next",
        }
    )
    assert body["session_id"] == "sid"
    assert "history" not in body
    assert body["user_id"] == "u1"
    assert body["user_dept"] == "Tech"
    assert body["message"] == "next"


def test_fallback_history_is_bounded_and_starts_on_user():
    body = _build({"history": HISTORY, "maxHistory": 3, "message": "x"})
    assert "session_id" not in body
    assert [m["role"] for m in body["history"]] == ["user", "assistant"]


def test_history_shapes_are_normalised():
    code = """
const api = require(process.argv[1]);
process.stdout.write(JSON.stringify(api.normalizeHistory([
  { role: "assistant", content: "dropped" },
  { role: "user", content: "keep" },
  { role: "assistant", content: 42 },
  null,
], 20)));
"""
    assert _run(code) == [{"role": "user", "content": "keep"}]


def test_session_query_is_url_encoded():
    code = 'process.stdout.write(JSON.stringify(require(process.argv[1]).sessionQuery("a b&c")));'
    assert _run(code) == "user_id=a%20b%26c"


def test_dates_are_localized_and_invalid_values_are_empty():
    code = """
const api = require(process.argv[1]);
process.stdout.write(JSON.stringify({
  zh: api.formatDate("2026-11-10T13:00:00+00:00", "zh"),
  en: api.formatDate("2026-11-10T13:00:00+00:00", "en"),
  bad: api.formatDate("not-a-date", "zh"),
  missing: api.formatDate("", "en"),
}));
"""
    data = _run(code)
    assert data["zh"]
    assert data["en"]
    assert data["bad"] == ""
    assert data["missing"] == ""
