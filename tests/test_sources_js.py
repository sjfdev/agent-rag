"""The citation preview URL builder, exercised through Node.

Skipped (not failed) when Node is unavailable, keeping the Python suite offline.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "static" / "sources.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(code, payload):
    result = subprocess.run(
        [NODE, "-e", code, str(SCRIPT), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _query(payload):
    code = """
const api = require(process.argv[1]);
process.stdout.write(JSON.stringify(api.previewQuery(JSON.parse(process.argv[2]))));
"""
    return _run(code, payload)


def test_query_carries_the_citation_fields():
    query = _query(
        {
            "source": "Tech/policy.md",
            "userId": "u1",
            "dept": "Tech",
            "page": 3,
            "sheet": "Q1",
        }
    )
    assert "source=Tech%2Fpolicy.md" in query
    assert "user_id=u1" in query
    assert "user_dept=Tech" in query
    assert "page=3" in query
    assert "sheet=Q1" in query
    assert "slide=" not in query


def test_query_drops_empty_values_and_encodes_reserved_chars():
    query = _query({"source": "a b&c.md", "page": None, "sheet": "", "dept": ""})
    assert query == "source=a%20b%26c.md"


def test_query_supports_multiple_departments_and_groups():
    query = _query(
        {"source": "x.md", "depts": ["Tech", "Finance"], "groups": ["exec", "group:hr"]}
    )
    assert "user_depts=Tech" in query
    assert "user_depts=Finance" in query
    assert "user_groups=exec" in query
    assert "user_groups=group%3Ahr" in query


def test_preview_path_without_arguments():
    code = """
const api = require(process.argv[1]);
process.stdout.write(JSON.stringify(api.previewPath({})));
"""
    result = subprocess.run(
        [NODE, "-e", code, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == "/sources/content"
