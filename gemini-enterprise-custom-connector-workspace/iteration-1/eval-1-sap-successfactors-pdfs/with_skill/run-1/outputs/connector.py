"""
SAP SuccessFactors HR policy PDF ingestion connector for Gemini Enterprise.

Orchestrates: Fetch (SuccessFactors OData) -> Transform (Document payload) ->
Sync (JSONL on GCS -> Discovery Engine import_documents).

Entrypoint for both initial FULL load and scheduled INCREMENTAL syncs.
Designed to run as a Cloud Run Job triggered by Cloud Scheduler.

Environment variables (set on the Cloud Run Job):
  PROJECT_ID                GCP project id
  LOCATION                  Discovery Engine location (typically "global")
  DATASTORE_ID              Existing datastore id (created by infra/create_datastore.py)
  IMS_ID                    Identity Mapping Store id (created by infra/create_datastore.py)
  STAGING_BUCKET            GCS bucket for JSONL staging
  WATERMARK_BLOB            GCS object that stores the high-water-mark timestamp
                            (e.g. "watermarks/successfactors_hr_policies.txt")
  SF_HOST                   SuccessFactors API host (e.g. "apisalesdemo2.successfactors.eu")
  SF_COMPANY_ID             SuccessFactors company / tenant id
  SF_OAUTH_TOKEN_URL        OAuth token endpoint
  SF_OAUTH_CLIENT_ID        OAuth client id (registered OAuth client in SF Admin Center)
  SF_OAUTH_USER_ID          Technical user the connector acts as (must have read on
                            DMS / Document categories that hold HR policies)
  SF_OAUTH_PRIVATE_KEY_SECRET  Secret Manager resource name for the SAML assertion
                               signing key OR client secret

  SYNC_MODE                 "FULL" (initial) or "INCREMENTAL" (default). The first
                            run for a fresh datastore must be FULL.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterable

from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import storage

from source_fetcher import SuccessFactorsClient, PolicyDocument
from transform import to_document
from identity_mapping import sync_identity_mappings_from_workday

logger = logging.getLogger("sf-hr-connector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
DATASTORE_ID = os.environ["DATASTORE_ID"]
IMS_ID = os.environ["IMS_ID"]
STAGING_BUCKET = os.environ["STAGING_BUCKET"]
WATERMARK_BLOB = os.environ.get("WATERMARK_BLOB", "watermarks/successfactors_hr_policies.txt")
SYNC_MODE = os.environ.get("SYNC_MODE", "INCREMENTAL").upper()


# -- Watermark helpers ---------------------------------------------------------

def _watermark_blob():
    return storage.Client().bucket(STAGING_BUCKET).blob(WATERMARK_BLOB)


def read_watermark() -> datetime | None:
    blob = _watermark_blob()
    if not blob.exists():
        return None
    raw = blob.download_as_text().strip()
    return datetime.fromisoformat(raw) if raw else None


def write_watermark(ts: datetime) -> None:
    _watermark_blob().upload_from_string(ts.isoformat(), content_type="text/plain")


# -- JSONL staging --------------------------------------------------------------

def write_jsonl_to_gcs(documents: list[discoveryengine.Document], blob_name: str) -> str:
    """Write Documents to a JSONL blob in GCS, return the gs:// URI."""
    jsonl = "\n".join(discoveryengine.Document.to_json(d, indent=None) for d in documents) + "\n"
    storage.Client().bucket(STAGING_BUCKET).blob(blob_name).upload_from_string(
        jsonl, content_type="application/json"
    )
    uri = f"gs://{STAGING_BUCKET}/{blob_name}"
    logger.info("Staged %d documents to %s", len(documents), uri)
    return uri


# -- Discovery Engine sync ------------------------------------------------------

def import_from_gcs(gcs_uri: str, mode: str) -> None:
    client = discoveryengine.DocumentServiceClient()
    parent = client.branch_path(
        project=PROJECT_ID, location=LOCATION,
        data_store=DATASTORE_ID, branch="default_branch",
    )
    reconciliation = (
        discoveryengine.ImportDocumentsRequest.ReconciliationMode.FULL
        if mode == "FULL"
        else discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL
    )
    op = client.import_documents(
        request=discoveryengine.ImportDocumentsRequest(
            parent=parent,
            gcs_source=discoveryengine.GcsSource(input_uris=[gcs_uri]),
            reconciliation_mode=reconciliation,
        )
    )
    logger.info("import_documents started (mode=%s, op=%s). Polling...", mode, op.operation.name)
    result = op.result(timeout=60 * 60)  # up to 1 hour for large initial loads
    logger.info("import_documents complete: %s", result)


# -- Batching helpers -----------------------------------------------------------

def _chunked(iterable, size):
    batch: list = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# -- Main orchestration ---------------------------------------------------------

def run():
    logger.info("Starting SuccessFactors HR connector. mode=%s", SYNC_MODE)

    # 1. Refresh identity mappings from Workday (the SSO source of truth).
    #    Workday holds the SAP user id <-> work email mapping. Doing this BEFORE
    #    ingesting documents ensures any new hires referenced in policy ACLs
    #    resolve correctly at query time.
    sync_identity_mappings_from_workday(
        project_id=PROJECT_ID, location=LOCATION, ims_id=IMS_ID,
    )

    # 2. Determine the high-water-mark for this run.
    if SYNC_MODE == "FULL":
        since = None
    else:
        since = read_watermark()
        if since is None:
            logger.warning("No watermark found; falling back to FULL sync.")

    run_started_at = datetime.now(timezone.utc)

    # 3. Fetch policy docs from SuccessFactors (paginated, OAuth-backed).
    sf = SuccessFactorsClient(
        host=os.environ["SF_HOST"],
        company_id=os.environ["SF_COMPANY_ID"],
        token_url=os.environ["SF_OAUTH_TOKEN_URL"],
        client_id=os.environ["SF_OAUTH_CLIENT_ID"],
        user_id=os.environ["SF_OAUTH_USER_ID"],
        signing_key_secret=os.environ["SF_OAUTH_PRIVATE_KEY_SECRET"],
    )

    policy_iter: Iterable[PolicyDocument] = sf.fetch_hr_policy_documents(
        modified_since=since if SYNC_MODE != "FULL" else None,
    )

    # 4. Transform + stage to GCS. Chunk to respect GCS-import limits:
    #    - max 100 files per request
    #    - max 100MB per file when dataSchema=content (typical for PDFs)
    #    Keeping ~200 PDFs per JSONL file leaves headroom on file size and stays
    #    well under the 100-file-per-request cap even with thousands of docs.
    DOCS_PER_FILE = 200
    staged_uris: list[str] = []
    total_docs = 0

    for shard_idx, batch in enumerate(_chunked(policy_iter, DOCS_PER_FILE)):
        documents = [to_document(p) for p in batch]
        blob_name = (
            f"imports/successfactors-hr/{run_started_at:%Y%m%dT%H%M%SZ}"
            f"/shard-{shard_idx:05d}.jsonl"
        )
        staged_uris.append(write_jsonl_to_gcs(documents, blob_name))
        total_docs += len(documents)

    if total_docs == 0:
        logger.info("No changes since %s; skipping import.", since)
        write_watermark(run_started_at)
        return

    # 5. Submit import. import_documents accepts up to 100 input_uris per request.
    #    Group staged shards into batches of 100 if we ever exceed that.
    effective_mode = "FULL" if (SYNC_MODE == "FULL" or since is None) else "INCREMENTAL"
    for uri_batch in _chunked(staged_uris, 100):
        # When sending multiple uris together, the API treats them as one job.
        # For simplicity here we issue per-uri imports; for very large initial
        # loads, switch to a single multi-uri request.
        for uri in uri_batch:
            import_from_gcs(uri, mode=effective_mode)

    # 6. Advance the watermark only after a successful incremental import.
    #    For FULL runs we still advance it so the next scheduled INCREMENTAL run
    #    has a sensible starting point.
    write_watermark(run_started_at)
    logger.info("Run complete. total_docs=%d watermark=%s", total_docs, run_started_at.isoformat())


if __name__ == "__main__":
    run()
