"""Thin client over the internal CMS REST API.

Assumptions
-----------
- Endpoint: GET {CMS_BASE_URL}?page=N&page_size=K&updated_since=ISO8601
- Auth: Bearer token in `Authorization` header.
- Response shape:
    {
      "items": [
        {
          "id": "...",
          "title": "...",
          "body_html": "...",
          "author": "...",
          "tags": ["..."],
          "url": "https://intranet/articles/<id>",
          "published_at": "2026-05-15T10:00:00Z",
          "updated_at":   "2026-05-15T10:00:00Z",
          "status": "published" | "draft" | "deleted"
        }
      ],
      "next_page": 2,           # null when finished
      "total":     49873
    }
"""

from __future__ import annotations

import logging
import time
from typing import Iterator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import CONFIG

log = logging.getLogger(__name__)


def _make_session() -> requests.Session:
    """Session with retry/backoff on 429 + 5xx."""
    s = requests.Session()
    retry = Retry(
        total=CONFIG.cms_max_retries,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=CONFIG.max_workers))
    s.headers.update({
        "Authorization": f"Bearer {CONFIG.cms_api_token}",
        "Accept": "application/json",
        "User-Agent": "cms-gemini-connector/1.0",
    })
    return s


def iter_articles(updated_since: Optional[str] = None) -> Iterator[dict]:
    """Yield every article (oldest -> newest) matching the watermark.

    `updated_since` is an ISO8601 string. If None -> full backfill.
    """
    session = _make_session()
    page = 1
    seen = 0

    while True:
        params = {"page": page, "page_size": CONFIG.cms_page_size}
        if updated_since:
            params["updated_since"] = updated_since

        t0 = time.monotonic()
        r = session.get(CONFIG.cms_base_url, params=params, timeout=CONFIG.cms_timeout_s)
        r.raise_for_status()
        payload = r.json()

        items = payload.get("items", [])
        log.info(
            "CMS page=%d fetched=%d total=%s elapsed=%.2fs",
            page, len(items), payload.get("total"), time.monotonic() - t0,
        )

        for item in items:
            seen += 1
            yield item

        next_page = payload.get("next_page")
        if not next_page:
            log.info("CMS pagination complete. total_yielded=%d", seen)
            return
        page = next_page
