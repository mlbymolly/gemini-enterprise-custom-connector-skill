# connector.py — orchestrate fetch -> transform -> GCS JSONL -> import
import os
import sys
import datetime
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import storage
from source_fetcher import fetch_kb_articles
from transform import to_document

PROJECT_ID = os.environ["GCP_PROJECT"]
LOCATION = os.environ.get("DE_LOCATION", "global")
DATASTORE_ID = os.environ["DE_DATASTORE_ID"]
BUCKET = os.environ["STAGING_BUCKET"]
WATERMARK_BLOB = "watermarks/snow_kb_last_run.txt"


def _read_watermark() -> str | None:
    blob = storage.Client().bucket(BUCKET).blob(WATERMARK_BLOB)
    return blob.download_as_text() if blob.exists() else None


def _write_watermark(ts: str) -> None:
    storage.Client().bucket(BUCKET).blob(WATERMARK_BLOB).upload_from_string(ts)


def main(mode: str = "INCREMENTAL"):
    since = None if mode == "FULL" else _read_watermark()
    docs, count = [], 0
    for row in fetch_kb_articles(since):
        docs.append(to_document(row))
        count += 1

    if not docs:
        print("no new articles; nothing to import")
        return

    # Write JSONL to GCS — production path.
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    blob_name = f"snow_kb/{ts}_{mode.lower()}.jsonl"
    jsonl = (
        "\n".join(
            discoveryengine.Document.to_json(d, indent=None) for d in docs
        )
        + "\n"
    )
    storage.Client().bucket(BUCKET).blob(blob_name).upload_from_string(
        jsonl, content_type="application/json"
    )
    gcs_uri = f"gs://{BUCKET}/{blob_name}"

    # Import.
    client = discoveryengine.DocumentServiceClient()
    parent = client.branch_path(PROJECT_ID, LOCATION, DATASTORE_ID, "default_branch")
    recon = (
        discoveryengine.ImportDocumentsRequest.ReconciliationMode.FULL
        if mode == "FULL"
        else discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL
    )
    op = client.import_documents(
        request=discoveryengine.ImportDocumentsRequest(
            parent=parent,
            gcs_source=discoveryengine.GcsSource(input_uris=[gcs_uri]),
            reconciliation_mode=recon,
        )
    )
    print(f"imported {count} docs via {gcs_uri}; op={op.operation.name}")

    # Advance the watermark only after import is submitted.
    _write_watermark(datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    main(mode=sys.argv[1] if len(sys.argv) > 1 else "INCREMENTAL")
