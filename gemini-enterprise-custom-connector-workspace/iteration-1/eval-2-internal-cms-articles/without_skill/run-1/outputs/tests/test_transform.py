"""Smoke tests for the transform layer."""
import base64

from connector.transform import to_document


def test_published_article_maps_to_document():
    art = {
        "id": 42,
        "title": "Hello",
        "body_html": "<p>Hi there</p>",
        "author": "Molly",
        "tags": ["news"],
        "url": "https://intranet/articles/42",
        "published_at": "2026-05-15T10:00:00Z",
        "updated_at":   "2026-05-15T10:00:00Z",
        "status": "published",
    }
    doc = to_document(art)
    assert doc["id"] == "42"
    assert doc["content"]["mimeType"] == "text/html"
    assert base64.b64decode(doc["content"]["rawBytes"]) == b"<p>Hi there</p>"
    assert doc["structData"]["title"] == "Hello"


def test_deleted_returns_delete_sentinel():
    doc = to_document({"id": 7, "status": "deleted"})
    assert doc == {"_delete": True, "id": "7"}


def test_draft_is_skipped():
    assert to_document({"id": 1, "status": "draft", "body_html": "x"}) is None


def test_empty_body_is_skipped():
    assert to_document({"id": 1, "status": "published", "body_html": "  "}) is None
