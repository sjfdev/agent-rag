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
THEME = ROOT / "static" / "theme.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SHARED_KEY = "ragBearer"
NAV_TARGETS = [
    "deptCard",
    "filesCard",
    "modelCard",
    "usersCard",
    "indexCard",
    "auditCard",
    "rebuildCard",
]
INLINE_SCRIPT = re.compile(r"<script(?:\s[^>]*)?>(.*?)</script>", re.S)


def test_both_pages_define_and_read_the_shared_bearer():
    for page in (ADMIN, INDEX):
        text = page.read_text()
        assert f'const SHARED_BEARER_KEY = "{SHARED_KEY}";' in text
        assert "authStore.get(SHARED_BEARER_KEY)" in text


def test_login_mirrors_and_logout_clears_the_shared_bearer():
    for page in (ADMIN, INDEX):
        text = page.read_text()
        assert "authStore.set(SHARED_BEARER_KEY, bearer);" in text
        assert "authStore.remove(SHARED_BEARER_KEY);" in text


def test_credentials_persist_across_tabs_via_local_storage():
    for page in (ADMIN, INDEX):
        text = page.read_text()
        assert "localStorage.getItem(key)" in text
        assert "localStorage.setItem(key, value);" in text
        assert "localStorage.removeItem(key);" in text
        # Older per-tab sessions must still be honoured once.
        assert "sessionStorage.getItem(key)" in text


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
                    ("dept", "files", "model", "accounts", "index",
                     "rebuild", "audit")}


def test_sidebar_is_hidden_until_authenticated():
    text = ADMIN.read_text()
    assert 'id="sidebar" hidden' in text
    assert '$("sidebar").hidden = true;' in text
    assert '$("sidebar").hidden = false;' in text


def test_hidden_cards_hide_their_menu_entry():
    text = ADMIN.read_text()
    assert "btn.hidden = !target || target.hidden;" in text
    assert 'data-target="usersCard" hidden' in text


def test_console_information_architecture():
    text = ADMIN.read_text()
    for key in ("admin.navGroup.content", "admin.navGroup.config", "admin.navGroup.ops"):
        assert f'data-i18n="{key}"' in text
    # Tabs expose semantics and keyboard switching.
    assert 'role="tablist"' in text
    assert 'role="tab"' in text
    assert '"ArrowDown"' in text
    # Header shows the current department, job indicator and folded status.
    assert 'id="currentDeptBadge"' in text
    assert 'id="jobPill"' in text
    assert 'class="statusPop"' in text
    # Retrieval replay is collapsed; model form has labels and a dirty badge.
    assert '<details class="searchBox">' in text
    assert 'id="modelDirty"' in text
    assert 'data-i18n="admin.modelBaseLabel"' in text
    assert 'data-i18n="admin.newUserLabel"' in text


def test_console_global_department_and_split_index_card():
    text = ADMIN.read_text()
    # The header owns the global department chooser.
    assert 'id="deptHeader"' in text
    assert "function changeDept(" in text
    # Index maintenance is its own section/card.
    assert 'id="indexCard"' in text
    assert 'data-target="indexCard"' in text
    assert 'id="indexMsg"' in text
    # Tables share skeleton / empty / error+retry primitives.
    assert "function renderSkeleton(" in text
    assert "function emptyRow(" in text
    assert "function errorMsg(" in text
    assert 't("common.retry")' in text


def test_console_p2_responsive_and_tables():
    text = ADMIN.read_text()
    # Below 1000px the menu becomes a sticky horizontal tab bar.
    assert "@media (max-width: 1000px)" in text
    assert "overflow-x: auto" in text
    # Panels expose a loading state and the header shows the last refresh.
    assert 'id="lastRefresh"' in text
    assert 'classList.toggle("loading"' in text
    # File table supports sorting and pagination.
    assert 'data-sort="mtime"' in text
    assert 'id="filePager"' in text
    assert "const FILE_PAGE_SIZE = 50;" in text
    # Audit can be filtered by time and text.
    assert 'id="auditSearch"' in text
    assert 'id="auditSince"' in text
    # Theme dialog has presets, live preview and a scope hint.
    assert 'id="themePresets"' in text
    assert "applyTheme(\"custom\", themeInputsValue())" in text
    assert 'data-i18n="admin.theme.scope"' in text


def test_admin_has_dark_light_and_custom_themes():
    text = ADMIN.read_text()
    assert '<script src="/ui/theme.js"></script>' in text
    assert 'id="theme"' in text
    assert 'id="themeModal"' in text
    assert 'value="dark"' in text and 'value="light"' in text and 'value="custom"' in text
    assert "applyTheme(" in text
    assert "loadCustomTheme()" in text
    assert "localStorage" in THEME.read_text()


def test_theme_script_parses():
    result = subprocess.run(
        [NODE, "--check", str(THEME)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_console_ux_guards():
    text = ADMIN.read_text()
    # Primary buttons derive their text color from the accent luminance.
    assert "color: var(--accent-fg)" in text
    assert '"--accent-fg"' in THEME.read_text()
    # Section choice is remembered and switching refreshes that panel.
    assert 'const SECTION_KEY = "adminSection";' in text
    assert "refreshSection(activeSection)" in text
    assert "rememberSection(activeSection);" in text
    # Wide tables scroll horizontally; results are mirrored in a toast.
    assert '<div class="tableWrap">' in text
    assert ".tableWrap { width: 100%; overflow-x: auto;" in text
    assert '<div class="toast" id="toast" role="status" aria-live="polite" hidden></div>' in text
    assert 'aria-live="polite"' in text


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


def test_console_uses_themed_confirmation_everywhere():
    text = ADMIN.read_text()
    assert 'id="confirmModal"' in text
    assert "function askConfirm(" in text
    assert "!confirm(" not in text
    assert "confirm(t(" not in text
    assert text.count("await askConfirm(") == 4


def test_running_ingest_job_is_restored():
    text = ADMIN.read_text()
    assert 'JOB_KEY = "adminJobId"' in text
    assert "await restoreJob();" in text
    assert "localStorage.removeItem(JOB_KEY)" in text


def test_document_title_follows_the_active_section():
    text = ADMIN.read_text()
    assert "function updateDocumentTitle()" in text
    assert "updateDocumentTitle();" in text


def test_upload_is_inline_with_the_file_list():
    text = ADMIN.read_text()
    assert 'id="uploadCard"' not in text
    assert 'id="uploadDept"' in text
    start = text.index('id="filesCard"')
    end = text.index('id="modelCard"')
    assert 'id="drop"' in text[start:end]
    assert 'id="uploadDept"' in text[start:end]


def test_header_groups_account_controls_and_merges_theme_edit():
    text = ADMIN.read_text()
    assert 'id="userMenu"' in text
    assert '<option value="custom-edit"' in text
    assert 'id="themeEdit"' not in text
    assert text.index('id="lang"') < text.index('id="userMenu"')


def test_chat_prevents_ime_send_and_duplicate_retry():
    text = INDEX.read_text()
    assert "e.isComposing" in text
    assert "send({ text, retry: true })" in text
    assert "sendBtn.onclick = () => send();" in text


def test_chat_aborts_on_context_switch_and_keeps_scroll_position():
    text = INDEX.read_text()
    assert "function startNewSession(key, vars) {\n  if (busy && abortController) abortController.abort();" in text
    assert 'id="scrollBottom"' in text
    assert "scrollChat(role === \"user\")" in text


def test_chat_exposes_live_status_and_dialog_focus():
    text = INDEX.read_text()
    assert 'id="srStatus"' in text
    assert 'aria-live="polite"' in text
    assert "previewReturnFocus" in text
    assert 'id="sessionClose"' in text


def test_chat_hides_dev_status_and_reads_identity_from_token():
    text = INDEX.read_text()
    assert "测试台" not in text
    assert 'class="chatStatus"' in text
    assert 'id="whoami"' in text
    assert 'id="chatTheme"' in text
    assert "deptSel.disabled = info.depts.length === 1" in text


def test_chat_citations_are_numbered_and_answers_have_actions():
    text = INDEX.read_text()
    assert "srchitNum" in text
    assert "box.open = sources.length <= 3;" in text
    assert "addActions(bubble, answer, { question: text })" in text
    assert "function regenerateAnswer(" in text


def test_chat_composer_debug_and_export_controls():
    text = INDEX.read_text()
    assert 'id="charCount"' in text
    assert "function updateCharCount()" in text
    assert 'id="debugToggle"' in text
    assert 'localStorage.getItem("chatDebug")' in text
    assert "function exportChat()" in text
    assert "function renderTimings(" in text


def test_sessions_search_loading_and_themed_confirmation():
    text = INDEX.read_text()
    assert 'id="sessionSearch"' in text
    assert 'id="sessionStatus"' in text
    assert 'id="sessionRetry"' in text
    assert "sessions.noMatch" in text
    assert 'id="confirm"' in text
    assert "askConfirm(" in text
    assert "window.confirm" not in text
