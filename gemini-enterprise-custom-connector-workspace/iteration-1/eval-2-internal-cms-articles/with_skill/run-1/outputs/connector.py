"""Entry point for the internal CMS articles connector.

Orchestrates: fetch from api.internal/articles -> transform to Document ->
write JSONL to GCS -> import_documents into the Discovery Engine datastore.

Watermark is read from / written to GCS at gs://{STAGING_BUCKET}/state/watermark.txt.
First run (no watermark file) does a FULL reconciliation; subsequent runs do
INCREMENTAL with WHERE updated_at > watermark semantics on the source side.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid

from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import storage

from source_fetcher import fetch_articles
from transform import to_document

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cms-connector")

PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
DATASTORE_ID = os.environ.get("DATASTORE_ID", "internal-cms-articles")
STAGING_BUCKET = os.environ["STAGING_BUCKET"]  # GCS bucket for JSONL shards
SHARD_SIZE = int(os.environ.get("SHARD_SIZE", "10000"))  # docs per JSONL file

WATERMARK_BLOB = "state/watermark.txt"


def read_watermark() -> dt.datetime | None:
    blob = storage.Client().bucket(STAGING_BUCKET).blob(WATERMARK_BLOB)
    if not blob.exists():
        return None
    return dt.datetime.fromisoformat(blob.download_as_text().strip())


def write_watermark(value: dt.datetime) -> None:
    blob = storage.Client().bucket(STAGING_BUCKET).blob(WATERMARK_BLOB)
    blob.upload_from_string(value.isoformat(), content_type="text/plain")


def write_jsonl_shards(documents, run_id: str) -> list[str]:
    """Write documents to GCS as JSONL, sharded for parallel import.

    Returns the list of gs:// URIs created. Honors the 100-files-per-request limit
    (we'll stay well under it). Each shard contains up to SHARD_SIZE documents.
    """
    bucket = storage.Client().bucket(STAGING_BUCKET)
    uris: list[str] = []
    shard: list[discoveryengine.Document] = []
    shard_idx = 0

    def flush():
        nonlocal shard, shard_idx
        if not shard:
            return
        blob_name = f"imports/{run_id}/shard-{shard_idx:05d}.jsonl"
        jsonl = "\n".join(discoveryengine.Document.to_json(d, indent=None) for d in shard) + "\n"
        bucket.blob(blob_name).upload_from_string(jsonl, content_type="application/json")
        uris.append(f"gs://{STAGING_BUCKET}/{blob_name}")
        log.info("Wrote %d docs to %s", len(shard), uris[-1])
        shard = []
        shard_idx += 1

    for d in documents:
        shard.append(d)
        if len(shard) >= SHARD_SIZE:
            flush()
    flush()
    return uris


def import_from_gcs(uris: list[str], mode: str) -> None:
    """Submit a single import request with all shards as input_uris.

    GCS imports allow up to 100 files per request, which comfortably covers 50k
    articles at SHARD_SIZE=10000 (5 shards) -- or even 500 shards if we ever shrink
    SHARD_SIZE. If we ever cross 100 shards, split into multiple requests.
    """
    if not uris:
        log.info("No shards to import.")
        return
    if len(uris) > 100:
        raise RuntimeError(
            f"{len(uris)} shards exceeds 100-file/request limit; split into multiple requests."
        )
    client = discoveryengine.DocumentServiceClient()
    parent = client.branch_path(
        project=PROJECT_ID, location=LOCATION,
        data_store=DATASTORE_ID, branch="default_branch",
    )
    recon = (
        discoveryengine.ImportDocumentsRequest.ReconciliationMode.FULL
        if mode == "FULL"
        else discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL
    )
    op = client.import_documents(
        request=discoveryengine.ImportDocumentsRequest(
            parent=parent,
            gcs_source=discoveryengine.GcsSource(input_uris=uris),
            reconciliation_mode=recon,
        )
    )
    log.info("Import LRO submitted (mode=%s, shards=%d). Waiting...", mode, len(uris))
    result = op.result()  # blocks until done
    log.info("Import done. Result metadata: %s", result)


def main() -> None:
    run_id = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    watermark = read_watermark()

    if watermark is None:
        log.info("No watermark found -> performing FULL initial sync.")
        mode = "FULL"
        since = None
    else:
        log.info("Watermark=%s -> performing INCREMENTAL sync.", watermark.isoformat())
        mode = "INCREMENTAL"
        since = watermark

    # Stream from source -> transform -> docs generator
    new_watermark = watermark or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    doc_count = 0

    def doc_iter():
        nonlocal new_watermark, doc_count
        for record in fetch_articles(since=since):
            updated = record["updated_at"]
            if updated > new_watermark:
                new_watermark = updated
            doc_count += 1
            yield to_document(record)

    uris = write_jsonl_shards(doc_iter(), run_id=run_id)
    log.info("Prepared %d documents across %d shard(s).", doc_count, len(uris))

    import_from_gcs(uris, mode=mode)

    # Advance watermark only on success.
    write_watermark(new_watermark)
    log.info("Sync complete. New watermark=%s. Run=%s", new_watermark.isoformat(), run_id)


if __name__ == "__main__":
    main()
