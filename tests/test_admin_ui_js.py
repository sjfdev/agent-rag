"""Shared login state and the console's section menu.

静态检查 `static/admin.html` / `static/index.html`：
`/admin` 与 `/ui/` 使用同一个共享 Bearer key，左侧分区菜单的跳转目标都存在，
侧边栏在登录后才显示。输入脚本再用 Node 做一次语法检查（无 Node 时跳过）。
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "static" / "admin.html"
INDEX = ROOT / "static" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SHARED_KEY = "ragBearer"
NAV_TARGETS = [
    "deptCard",
    "uploadCard",
    "filesCard",
    "modelCard",
    "usersCard",
    "rebuildCard",
    "auditCard",
]
INLINE_SCRIPT = re.compile(r"<script(?:\s[^>]*)?>(.*?)</script>", re.S)


def test_both_pages_define_and_read_the_shared_bearer():
    for page in (ADMIN, INDEX):
        text = page.read_text()
        assert f'const SHARED_BEARER_KEY = "{SHARED_KEY}";' in text
        assert "sessionStorage.getItem(SHARED_BEARER_KEY)" in text


def test_login_mirrors_and_logout_clears_the_shared_bearer():
    for page in (ADMIN, INDEX):
        text = page.read_text()
        assert "sessionStorage.setItem(SHARED_BEARER_KEY, bearer);" in text
        assert "sessionStorage.removeItem(SHARED_BEARER_KEY);" in text


def test_menu_targets_match_existing_cards():
    text = ADMIN.read_text()
    targets = re.findall(r'data-target="([^"]+)"', text)
    card_ids = re.findall(r'<section class="card[^"]*" id="([^"]+)"', text)
    assert targets == NAV_TARGETS
    assert set(targets) <= set(card_ids)


def test_menu_entries_use_bilingual_nav_keys():
    text = ADMIN.read_text()
    keys = set(re.findall(r'data-i18n="(admin\.nav\.[^"]+)"', text))
    assert keys == {f"admin.nav.{name}" for name in
                    ("dept", "upload", "files", "model", "accounts", "rebuild",
                     "audit")}


def test_sidebar_is_hidden_until_authenticated():
    text = ADMIN.read_text()
    assert 'id="sidebar" hidden' in text
    assert '$("sidebar").hidden = true;' in text
    assert '$("sidebar").hidden = false;' in text


def test_hidden_cards_hide_their_menu_entry():
    text = ADMIN.read_text()
    assert "btn.hidden = !target || target.hidden;" in text
    assert 'id="navUsers" data-target="usersCard" data-i18n="admin.nav.accounts" hidden' in text


def test_inline_scripts_parse():
    for page in (ADMIN, INDEX):
        scripts = "\n".join(INLINE_SCRIPT.findall(page.read_text()))
        assert scripts.strip(), page.name
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(scripts)
            path = handle.name
        result = subprocess.run(
            [NODE, "--check", path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, f"{page.name}: {result.stderr}"
