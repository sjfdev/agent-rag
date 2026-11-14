"""The chat page's citation-preview wiring."""

from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "static" / "index.html"


def test_page_wires_the_preview_modal():
    text = PAGE.read_text()
    for needle in (
        'id="preview"',
        'id="previewBody"',
        "/ui/sources.js",
        "ChatSources.previewPath",
        "openSourcePreview(",
        "chat.preview.hint",
    ):
        assert needle in text


def test_sources_helper_is_served():
    import main

    with TestClient(main.app) as client:
        resp = client.get("/ui/sources.js")
        assert resp.status_code == 200
        assert "previewPath" in resp.text
