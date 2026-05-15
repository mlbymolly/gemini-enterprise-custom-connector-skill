"""Watermark / checkpoint storage.

Stores the max(updated_at) of the last successful run so the next run only
pulls deltas. GCS is the default; local JSON is supported for dev.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from .config import CONFIG

log = logging.getLogger(__name__)


def _is_gcs(uri: str) -> bool:
    return uri.startswith("gs://")


def _gcs_parts(uri: str):
    bucket, _, key = uri[len("gs://"):].partition("/")
    return bucket, key


def read_watermark() -> Optional[str]:
    try:
        if _is_gcs(CONFIG.state_uri):
            from google.cloud import storage
            bucket_name, key = _gcs_parts(CONFIG.state_uri)
            blob = storage.Client().bucket(bucket_name).blob(key)
            if not blob.exists():
                return None
            return json.loads(blob.download_as_text()).get("updated_since")
        with open(CONFIG.state_uri, "r", encoding="utf-8") as f:
            return json.load(f).get("updated_since")
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("watermark read failed: %s (treating as cold start)", e)
        return None


def write_watermark(iso_ts: str) -> None:
    payload = json.dumps({"updated_since": iso_ts, "written_at": _now_iso()})
    if _is_gcs(CONFIG.state_uri):
        from google.cloud import storage
        bucket_name, key = _gcs_parts(CONFIG.state_uri)
        storage.Client().bucket(bucket_name).blob(key).upload_from_string(
            payload, content_type="application/json",
        )
    else:
        with open(CONFIG.state_uri, "w", encoding="utf-8") as f:
            f.write(payload)
    log.info("watermark advanced to %s", iso_ts)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
