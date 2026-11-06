"""The zh/en dictionaries and the keys referenced by the two UIs.

`static/i18n.js` is plain browser JavaScript with no build step. This test
loads it under Node (like tests/test_markdown_js.py does for markdown.js), then
checks that both languages define the same keys with the same placeholders and
that every data-i18n attribute in index.html/admin.html resolves.

Skipped (not failed) when Node is unavailable, so the offline Python suite
keeps working on machines without it.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "static" / "i18n.js"
PAGES = [ROOT / "static" / "index.html", ROOT / "static" / "admin.html"]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PLACEHOLDER = re.compile(r"\{(\w+)\}")
DATA_KEY = re.compile(r'data-i18n(?:-placeholder|-title|-aria-label)?="([^"]+)"')
JS_KEY = re.compile(r'\bt\("([^"]+)"')


def _dictionaries():
    harness = """
const api = require(process.argv[1]);
api.setLanguage("zh");
const zhInterpolated = {
  sources: api.t("chat.sources", { count: 3 }),
  fileDeleted: api.t("admin.fileDeleted", { dept: "Demo", source: "a.pdf", chunks: 2 }),
};
api.setLanguage("en");
const enInterpolated = {
  sources: api.t("chat.sources", { count: 3 }),
  fileDeleted: api.t("admin.fileDeleted", { dept: "Demo", source: "a.pdf", chunks: 2 }),
};
process.stdout.write(JSON.stringify({
  zh: api.TRANSLATIONS.zh,
  en: api.TRANSLATIONS.en,
  zhInterpolated,
  enInterpolated,
}));
"""
    result = subprocess.run(
        [NODE, "-e", harness, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_dictionaries_have_the_same_keys():
    data = _dictionaries()
    assert set(data["zh"]) == set(data["en"])


def test_no_empty_translations():
    data = _dictionaries()
    for lang in ("zh", "en"):
        empty = [key for key, value in data[lang].items() if not value.strip()]
        assert empty == []


def test_placeholders_match_across_languages():
    data = _dictionaries()
    for key, zh_value in data["zh"].items():
        zh_vars = sorted(PLACEHOLDER.findall(zh_value))
        en_vars = sorted(PLACEHOLDER.findall(data["en"][key]))
        assert zh_vars == en_vars, key


def test_interpolation_uses_the_selected_language():
    data = _dictionaries()
    assert data["zhInterpolated"]["sources"] == "来源（3）"
    assert data["enInterpolated"]["sources"] == "Sources (3)"
    assert data["zhInterpolated"]["fileDeleted"] == "已删除 Demo/a.pdf（移除 2 个 chunk）"
    assert data["enInterpolated"]["fileDeleted"] == "Deleted Demo/a.pdf (2 chunks removed)"


def test_pages_only_reference_known_keys():
    data = _dictionaries()
    known = set(data["zh"])
    for page in PAGES:
        text = page.read_text()
        keys = set(DATA_KEY.findall(text)) | set(JS_KEY.findall(text))
        missing = sorted(keys - known)
        assert missing == [], f"{page.name}: {missing}"


def test_translate_dom_handles_vars_and_attributes():
    harness = """
const api = require(process.argv[1]);
function el(attrs) {
  return {
    attrs: Object.assign({}, attrs),
    textContent: "",
    getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
}
const summary = el({
  "data-i18n": "chat.sources",
  "data-i18n-vars": JSON.stringify({ count: 2 }),
});
const input = el({ "data-i18n-placeholder": "auth.username" });
const link = el({ "data-i18n-title": "chat.newChatTitle" });
const dom = { querySelectorAll: () => [summary, input, link] };
api.setLanguage("en");
api.translateDom(dom);
process.stdout.write(JSON.stringify({
  text: summary.textContent,
  placeholder: input.attrs.placeholder,
  title: link.attrs.title,
}));
"""
    result = subprocess.run(
        [NODE, "-e", harness, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "text": "Sources (2)",
        "placeholder": "Username",
        "title": "Open a fresh session in a new tab",
    }
