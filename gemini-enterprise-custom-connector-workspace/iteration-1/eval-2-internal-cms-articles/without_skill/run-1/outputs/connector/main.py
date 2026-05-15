"""Entrypoint: hourly Cloud Run Job / Cloud Scheduler target.

Flow:
  1. Read watermark (last successful updated_at).
  2. Page through CMS for items updated after the watermark.
  3. Transform -> upsert/delete in Discovery Engine in batches.
  4. Advance the watermark on success.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import List

from .cms_client import iter_articles
from .config import CONFIG
from .gemini_sink import GeminiSink
from .state import read_watermark, write_watermark
from .transform import to_document

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("cms-connector")


def run() -> int:
    sink = GeminiSink()

    watermark = None if CONFIG.run_mode == "full" else read_watermark()
    log.info("starting run mode=%s watermark=%s", CONFIG.run_mode, watermark)

    upsert_buf: List[dict] = []
    delete_buf: List[str] = []
    max_updated_at = watermark
    counts = {"seen": 0, "upserts": 0, "deletes": 0, "skipped": 0}

    for article in iter_articles(updated_since=watermark):
        counts["seen"] += 1
        if article.get("updated_at") and (
            max_updated_at is None or article["updated_at"] > max_updated_at
        ):
            max_updated_at = article["updated_at"]

        doc = to_document(article)
        if doc is None:
            counts["skipped"] += 1
            continue
        if doc.get("_delete"):
            delete_buf.append(doc["id"])
        else:
            upsert_buf.append(doc)

        if len(upsert_buf) >= CONFIG.import_batch_size:
            sink.upsert_batch(upsert_buf)
            counts["upserts"] += len(upsert_buf)
            upsert_buf.clear()
        if len(delete_buf) >= CONFIG.import_batch_size:
            sink.delete_ids(delete_buf)
            counts["deletes"] += len(delete_buf)
            delete_buf.clear()

    # Flush remainders.
    if upsert_buf:
        sink.upsert_batch(upsert_buf)
        counts["upserts"] += len(upsert_buf)
    if delete_buf:
        sink.delete_ids(delete_buf)
        counts["deletes"] += len(delete_buf)

    # Advance watermark only on full success; bump by 1s to avoid re-fetch.
    if max_updated_at:
        write_watermark(max_updated_at)
    else:
        # No data this run: leave previous watermark intact, but record heartbeat.
        log.info("no new articles; watermark unchanged: %s", watermark)

    log.info("run finished: %s", counts)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:
        log.exception("connector run failed")
        sys.exit(1)
