"""The chat UI's Markdown renderer, exercised through Node.

`static/markdown.js` turns model output into HTML, which makes its escaping
rules security-relevant. The page loads it with a plain <script> tag, and this
test runs the same file under Node with a small assertion harness.

Skipped (not failed) when Node is unavailable, so the offline Python suite keeps
working on machines without it; CI installs Node indirectly via the runner
image, but the skip keeps that assumption out of the dependency graph.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "static" / "markdown.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _render(cases):
    """Render each input in one Node process and return the outputs."""
    # With `node -e`, process.argv[1] is the first argument after the script.
    harness = """
const { renderMarkdown } = require(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(cases.map((c) => renderMarkdown(c))));
"""
    result = subprocess.run(
        [NODE, "-e", harness, str(SCRIPT), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_escapes_raw_html():
    (out,) = _render(["<script>alert(1)</script>"])
    assert "<script" not in out
    assert "&lt;script&gt;" in out


def test_escapes_img_onerror():
    (out,) = _render(['<img src=x onerror="alert(1)">'])
    assert "<img" not in out
    assert "onerror=\"alert(1)\"" not in out


def test_renders_bold_and_lists():
    (out,) = _render(["**bold**\n- one\n- two"])
    assert "<strong>bold</strong>" in out
    assert out.count("<li>") == 2


def test_renders_a_table():
    (out,) = _render(["| A | B |\n|---|---|\n| 1 | 2 |"])
    assert "<th>A</th>" in out
    assert "<td>1</td>" in out


def test_keeps_safe_links():
    (out,) = _render(["[docs](https://example.com/a?b=1)"])
    assert 'href="https://example.com/a?b=1"' in out
    assert 'rel="noopener noreferrer nofollow"' in out


@pytest.mark.parametrize(
    "payload",
    [
        "[click](javascript:alert(1))",
        "[click](JAVASCRIPT:alert(1))",
        "[x](data:text/html,<script>alert(1)</script>)",
        "[v](vbscript:msgbox)",
        "[r](/admin)",
        "[p](//evil.example.com)",
    ],
)
def test_dangerous_schemes_never_become_anchors(payload):
    """Disallowed targets stay readable text; they must never be clickable."""
    (out,) = _render([payload])
    assert "<a " not in out.lower()
    assert "<script" not in out.lower()


def test_javascript_scheme_is_visible_as_text_not_a_link():
    (out,) = _render(["[click](javascript:alert(1))"])
    # The label survives so the reader can still see what the model wrote.
    assert "click" in out
    assert "&lt;" not in out or "a" in out


def test_code_fence_content_is_not_parsed():
    (out,) = _render(["```js\n<script>alert(1)</script>\n**not bold**\n```"])
    assert 'class="language-js"' in out
    assert "<script" not in out
    assert "<strong>" not in out
    assert "&lt;script&gt;" in out


def test_inline_code_is_literal():
    (out,) = _render(["use `<div>` and `**x**`"])
    assert '<code>&lt;div&gt;</code>' in out
    assert "<strong>" not in out


def test_headings_are_demoted_below_page_headings():
    (out,) = _render(["# Title"])
    assert "<h4>Title</h4>" in out
    assert "<h1>" not in out


def test_plain_text_is_not_double_escaped():
    (out,) = _render(["a < b & c"])
    assert "a &lt; b &amp; c" in out
    assert "&amp;lt;" not in out


def test_link_label_is_escaped_too():
    (out,) = _render(["[<img onerror=1>](https://example.com)"])
    assert "<img" not in out
    assert "&lt;img" in out
