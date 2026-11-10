"""The chat page's server-session wiring.

The behavior of `static/sessions.js` is covered under Node; these checks make
sure the page actually loads it, renders the sidebar, and advertises the
capability through `/info`.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from src.config import settings

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "static" / "index.html"


def test_page_has_the_session_sidebar():
    text = PAGE.read_text()
    for needle in (
        'id="sessionBar"',
        'id="sessionList"',
        'id="newSession"',
        'id="sessionToggle"',
        "/ui/sessions.js",
        "ChatSessions.buildChatBody",
        "session_id",
        "refreshSessions()",
        "openSession(",
    ):
        assert needle in text


def test_sessions_helper_is_served():
    import main

    with TestClient(main.app) as client:
        resp = client.get("/ui/sessions.js")
        assert resp.status_code == 200
        assert "buildChatBody" in resp.text


def test_info_exposes_session_settings(monkeypatch):
    import main

    monkeypatch.setattr(settings, "SESSIONS_ENABLED", False)
    monkeypatch.setattr(settings, "HISTORY_MAX_MESSAGES", 7)
    with TestClient(main.app) as client:
        body = client.get("/info").json()
    assert body["sessions_enabled"] is False
    assert body["history_max_messages"] == 7
