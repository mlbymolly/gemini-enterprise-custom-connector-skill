"""CMS row -> Discovery Engine Document.

Gemini Enterprise (Discovery Engine / Vertex AI Search) expects
"unstructured" or "structured" documents. For long-form articles we use the
unstructured form so that semantic chunking + grounding work out of the box.

Reference schema:
  {
    "id": "<stable id>",
    "schemaId": "default_schema",
    "content": {
      "mimeType": "text/html",
      "rawBytes": "<base64 of body_html>"
    },
    "structData": { ...metadata that we want filterable/displayable... }
  }
"""

from __future__ import annotations

import base64
import html
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(body_html: str) -> str:
    """Cheap HTML -> text fallback used only for length checks / logging."""
    return html.unescape(_TAG_RE.sub(" ", body_html or "")).strip()


def to_document(article: dict) -> Optional[dict]:
    """Map a CMS article into a Discovery Engine Document dict.

    Returns None if the article should be skipped (drafts, empty bodies).
    Deleted articles return a sentinel dict with `_delete=True` so the
    caller can route them to deleteDocuments().
    """
    status = article.get("status", "published")
    article_id = str(article["id"])

    if status == "deleted":
        return {"_delete": True, "id": article_id}

    if status != "published":
        log.debug("skip non-published article id=%s status=%s", article_id, status)
        return None

    body_html = article.get("body_html") or ""
    if not _strip_html(body_html):
        log.warning("skip empty-body article id=%s", article_id)
        return None

    return {
        "id": article_id,
        "schemaId": "default_schema",
        "content": {
            "mimeType": "text/html",
            "rawBytes": base64.b64encode(body_html.encode("utf-8")).decode("ascii"),
        },
        "structData": {
            "title": article.get("title", ""),
            "author": article.get("author", ""),
            "tags": article.get("tags", []),
            "url": article.get("url", ""),
            "published_at": article.get("published_at"),
            "updated_at": article.get("updated_at"),
        },
    }
