"""Paginated reads from the internal CMS REST API at api.internal/articles.

Assumed API contract (state this assumption to the user — confirm against your real API):

  GET https://api.internal/articles
    ?updated_since=<ISO8601>   # optional; omitted for full sync
    &page_size=<int>
    &page_token=<opaque>       # cursor pagination

Response JSON:
  {
    "articles": [
      {
        "id": "<int|string>",            # stable CMS primary key
        "title": "...",
        "body_html": "<html>...</html>",
        "url": "https://cms.internal/articles/123",
        "author_email": "alice@corp.com",
        "tags": ["howto", "billing"],
        "updated_at": "2026-05-15T10:23:00Z",
        "reader_groups": ["engineering", "support"],   # internal group names
        "reader_users":  ["bob@corp.com"],             # explicit user grants
        "is_public":     false                          # if true: open to all employees
      },
      ...
    ],
    "next_page_token": "<opaque-or-null>"
  }

Auth is via a service-account API key set in env var CMS_API_KEY.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Iterator

import requests

log = logging.getLogger(__name__)

BASE_URL = os.environ.get("CMS_BASE_URL", "https://api.internal/articles")
API_KEY = os.environ["CMS_API_KEY"]
PAGE_SIZE = int(os.environ.get("CMS_PAGE_SIZE", "500"))
TIMEOUT_S = 30
MAX_RETRIES = 5


def _get_with_retry(params: dict) -> dict:
    """GET with exponential backoff for 429/5xx."""
    backoff = 1.0
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
                BASE_URL,
                params=params,
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=TIMEOUT_S,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"transient {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if attempt == MAX_RETRIES - 1:
                raise
            log.warning("CMS fetch attempt %d failed (%s); backing off %.1fs", attempt + 1, e, backoff)
            time.sleep(backoff)
            backoff *= 2


def fetch_articles(since: dt.datetime | None = None) -> Iterator[dict]:
    """Yield article records from the CMS, paginating until exhausted.

    If `since` is None -> full sync (no updated_since filter).
    Otherwise -> only articles modified strictly after `since`.

    Yields parsed records with `updated_at` already coerced to a tz-aware datetime
    so connector.py can compute the new watermark cleanly.
    """
    params: dict[str, str | int] = {"page_size": PAGE_SIZE}
    if since is not None:
        params["updated_since"] = since.isoformat()

    page = 0
    while True:
        payload = _get_with_retry(params)
        articles = payload.get("articles", [])
        log.info("Page %d: fetched %d article(s)", page, len(articles))
        for a in articles:
            # Normalize updated_at to a tz-aware datetime.
            a["updated_at"] = dt.datetime.fromisoformat(a["updated_at"].replace("Z", "+00:00"))
            yield a

        token = payload.get("next_page_token")
        if not token:
            return
        params["page_token"] = token
        page += 1
