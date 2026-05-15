"""Cloud Run Job entry point. Runs one ingestion pass and exits.

Modes:
  --mode initial        FULL reconciliation, no watermark filter.
  --mode incremental    INCREMENTAL reconciliation, filter by watermark.
  --mode reconcile      FULL reconciliation, ignores watermark — weekly drift catcher.

Watermark is persisted in Firestore (collection `ingestion_watermarks`, doc id
matches --watermark-key). If the watermark doc is missing, behaves as initial.

Example invocation as a Cloud Run Job:

  python connector.py \\
      --mode incremental \\
      --watermark-key databricks-policies \\
      --volume-root /Volumes/main/docs/policies \\
      --gcs-staging gs://my-staging/databricks-policies \\
      --project my-gcp-project \\
      --datastore-id databricks-docs \\
      --default-reader-group databricks_readers
"""

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone

from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import firestore, storage

from databricks_fetcher import fetch_volume_files, fetch_table_rows
from transform import file_record_to_document, table_row_to_document

PROJECT_ID = os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("DISCOVERY_ENGINE_LOCATION", "global")
GCS_FILE_CAP = 100  # API limit per import request


def get_watermark(key: str) -> datetime | None:
    doc = firestore.Client().collection("ingestion_watermarks").document(key).get()
    if not doc.exists:
        return None
    iso = doc.to_dict().get("watermark")
    return datetime.fromisoformat(iso) if iso else None


def put_watermark(key: str, value: datetime) -> None:
    firestore.Client().collection("ingestion_watermarks").document(key).set(
        {"watermark": value.isoformat(), "updated_at": datetime.now(timezone.utc).isoformat()}
    )


def write_jsonl_shards(documents, bucket: str, prefix: str, batch_size: int = 1000):
    """Write one JSONL shard per `batch_size` documents. Returns list of GS URIs."""
    client = storage.Client().bucket(bucket)
    uris = []
    buf: list[str] = []
    shard_idx = 0
    run_id = uuid.uuid4().hex[:8]

    def flush():
        nonlocal shard_idx, buf
        if not buf:
            return
        blob_name = f"{prefix}/{run_id}/shard-{shard_idx:05d}.jsonl"
        client.blob(blob_name).upload_from_string(
            "\n".join(buf) + "\n", content_type="application/json"
        )
        uris.append(f"gs://{bucket}/{blob_name}")
        shard_idx += 1
        buf = []

    for d in documents:
        buf.append(discoveryengine.Document.to_json(d, indent=None))
        if len(buf) >= batch_size:
            flush()
    flush()
    return uris


def import_from_gcs(
    project: str, datastore_id: str, uris: list[str], mode: str
):
    client = discoveryengine.DocumentServiceClient()
    parent = client.branch_path(
        project=project,
        location=LOCATION,
        data_store=datastore_id,
        branch="default_branch",
    )
    recon = (
        discoveryengine.ImportDocumentsRequest.ReconciliationMode.FULL
        if mode in ("initial", "reconcile")
        else discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL
    )

    # API caps at 100 input_uris per request — chunk if needed.
    operations = []
    for i in range(0, len(uris), GCS_FILE_CAP):
        chunk = uris[i : i + GCS_FILE_CAP]
        op = client.import_documents(
            request=discoveryengine.ImportDocumentsRequest(
                parent=parent,
                gcs_source=discoveryengine.GcsSource(input_uris=chunk),
                reconciliation_mode=recon,
            )
        )
        operations.append(op)

    for op in operations:
        result = op.result(timeout=3600)
        print(f"[import] done: {result}")


def documents_from_volume(args, since: datetime | None):
    for fr in fetch_volume_files(
        volume_root=args.volume_root,
        since=since,
        default_readers_groups=args.default_reader_group or [],
    ):
        yield file_record_to_document(fr)


def documents_from_table(args, since: datetime | None):
    for tr in fetch_table_rows(
        http_path=args.http_path,
        catalog=args.table_catalog,
        schema=args.table_schema,
        table=args.table_name,
        primary_key=args.table_pk,
        columns_for_text=args.text_columns,
        columns_for_metadata=args.metadata_columns,
        watermark_column=args.watermark_column,
        since=since,
        default_readers_groups=args.default_reader_group or [],
    ):
        yield table_row_to_document(tr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["initial", "incremental", "reconcile"], required=True)
    p.add_argument("--watermark-key", required=True)
    p.add_argument("--gcs-staging", required=True, help="gs://bucket/prefix")
    p.add_argument("--project", default=PROJECT_ID, required=PROJECT_ID is None)
    p.add_argument("--datastore-id", required=True)
    p.add_argument("--default-reader-group", action="append", default=[])

    # UC volume source (optional)
    p.add_argument("--volume-root", help="/Volumes/<catalog>/<schema>/<volume>")

    # UC table source (optional)
    p.add_argument("--http-path", help="SQL warehouse HTTP path")
    p.add_argument("--table-catalog")
    p.add_argument("--table-schema")
    p.add_argument("--table-name")
    p.add_argument("--table-pk")
    p.add_argument("--text-columns", nargs="+", default=[])
    p.add_argument("--metadata-columns", nargs="+", default=[])
    p.add_argument("--watermark-column", help="row column used as the watermark, e.g. updated_at")

    args = p.parse_args()

    if not (args.volume_root or args.table_name):
        p.error("provide --volume-root, or all of --table-catalog/--table-schema/--table-name/--table-pk")

    since = None
    if args.mode == "incremental":
        since = get_watermark(args.watermark_key)
        print(f"[watermark] resuming from {since}")

    if not args.gcs_staging.startswith("gs://"):
        p.error("--gcs-staging must be gs://bucket/prefix")
    bucket, _, prefix = args.gcs_staging[len("gs://") :].partition("/")

    def all_docs():
        if args.volume_root:
            yield from documents_from_volume(args, since)
        if args.table_name:
            yield from documents_from_table(args, since)

    started = time.time()
    uris = write_jsonl_shards(all_docs(), bucket=bucket, prefix=prefix or "ingest")
    if not uris:
        print("[import] no documents — skipping import_documents")
    else:
        print(f"[import] {len(uris)} shard(s) → mode={args.mode}")
        import_from_gcs(args.project, args.datastore_id, uris, args.mode)

    # Advance the watermark only on success.
    put_watermark(args.watermark_key, datetime.now(timezone.utc))
    print(f"[done] elapsed {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
