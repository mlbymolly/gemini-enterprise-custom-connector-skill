"""Sync HR policy PDFs from SAP SuccessFactors to a Gemini Enterprise data store.

Steps per run:
  1. Read the last high-water mark from gs://$BUCKET/state/last_sync.txt.
  2. Query SuccessFactors for HR policies modified since then (MDF object cust_HRPolicy).
  3. For each policy, download its attachment, resolve its RBP group ACL.
  4. Upload PDF -> gs://$BUCKET/pdfs/<policyId>.pdf.
  5. Emit metadata.jsonl describing every policy.
  6. ImportDocuments(reconciliation_mode=FULL) into the data store.
  7. Write a new high-water mark.

The high-water mark is for observability/logging; reconciliation_mode=FULL means
the import is a complete replacement based on the current run's contents.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import sys

from google.cloud import discoveryengine_v1 as de
from google.cloud import storage

from successfactors_client import SFConfig, SuccessFactorsClient

PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
DATA_STORE_ID = os.environ["DATA_STORE_ID"]
BUCKET = os.environ["BUCKET"]
SF_API_HOST = os.environ["SF_API_HOST"]
SF_COMPANY_ID = os.environ["SF_COMPANY_ID"]
SF_CLIENT_ID = os.environ["SF_CLIENT_ID"]
SF_API_USER = os.environ["SF_API_USER"]
SF_PRIVATE_KEY = os.environ["SF_PRIVATE_KEY_PEM"].encode("utf-8")
SF_CERT = os.environ["SF_CERT_PEM"].encode("utf-8")

# Adjust the MDF entity + ACL field names for your tenant.
POLICY_ENTITY = os.environ.get("SF_POLICY_ENTITY", "cust_HRPolicy")
POLICY_SELECT = [
    "externalCode",          # we use this as the stable doc ID
    "cust_title",
    "cust_summary",
    "cust_effectiveDate",
    "cust_locale",
    "cust_owner",
    "cust_attachmentId_attachment_attachmentId",  # joins to Attachment
    "cust_visibleToGroups",  # comma-separated list of RBP group IDs
    "lastModifiedDateTime",
]


def _gcs_blob(client: storage.Client, path: str) -> storage.Blob:
    return client.bucket(BUCKET).blob(path)


def _read_high_water_mark(client: storage.Client) -> str | None:
    blob = _gcs_blob(client, "state/last_sync.txt")
    if not blob.exists():
        return None
    return blob.download_as_text().strip()


def _write_high_water_mark(client: storage.Client, ts: str) -> None:
    _gcs_blob(client, "state/last_sync.txt").upload_from_string(ts)


def _odata_filter_since(ts: str | None) -> str | None:
    if not ts:
        return None
    # SF wants the literal datetime in OData format.
    return f"lastModifiedDateTime ge datetimeoffset'{ts}'"


def _resolve_acl(visible_to_groups: str | None) -> de.Document.AclInfo:
    """Convert a comma-separated list of RBP group IDs into Discovery Engine ACL info.

    If a policy is intended to be visible to everyone in the organization, you can
    return an idp_wide reader instead.
    """
    if not visible_to_groups:
        # Default to "no one" if nothing is set; safer than implicit open.
        return de.Document.AclInfo(readers=[de.Document.AclInfo.AccessRestriction(
            principals=[]
        )])
    principals = []
    for group_id in (g.strip() for g in visible_to_groups.split(",") if g.strip()):
        principals.append(de.Principal(
            external_entity_id=f"external_group:{group_id}"
        ))
    return de.Document.AclInfo(
        readers=[de.Document.AclInfo.AccessRestriction(principals=principals)]
    )


def _build_documents(
    sf: SuccessFactorsClient,
    storage_client: storage.Client,
    since: str | None,
) -> list[de.Document]:
    docs: list[de.Document] = []
    bucket = storage_client.bucket(BUCKET)
    for policy in sf.iter_entity(
        POLICY_ENTITY,
        select=POLICY_SELECT,
        filter_expr=_odata_filter_since(since),
    ):
        doc_id = policy["externalCode"]
        attachment_id = policy.get("cust_attachmentId_attachment_attachmentId")
        if not attachment_id:
            continue
        pdf_bytes, att_meta = sf.fetch_attachment_bytes(str(attachment_id))

        # Stage PDF in GCS.
        pdf_blob = bucket.blob(f"pdfs/{doc_id}.pdf")
        pdf_blob.upload_from_file(
            io.BytesIO(pdf_bytes), content_type="application/pdf"
        )
        gcs_uri = f"gs://{BUCKET}/pdfs/{doc_id}.pdf"

        struct_data = {
            "title": policy.get("cust_title") or att_meta.get("fileName"),
            "summary": policy.get("cust_summary"),
            "effective_date": policy.get("cust_effectiveDate"),
            "locale": policy.get("cust_locale"),
            "owner_sap_user_id": policy.get("cust_owner"),
            "source_system": "sap_successfactors",
            "source_url": (
                f"https://{SF_API_HOST.replace('api', 'performancemanager')}"
                f"/sf/successfactors?company={SF_COMPANY_ID}#policy/{doc_id}"
            ),
            "last_modified": policy.get("lastModifiedDateTime"),
        }

        doc = de.Document(
            id=doc_id,
            content=de.Document.Content(
                mime_type="application/pdf",
                uri=gcs_uri,
            ),
            struct_data=struct_data,  # promoted into metadata for filtering / citations
            acl_info=_resolve_acl(policy.get("cust_visibleToGroups")),
        )
        docs.append(doc)
    return docs


def _stage_metadata_jsonl(
    storage_client: storage.Client,
    docs: list[de.Document],
) -> str:
    lines: list[str] = []
    for d in docs:
        lines.append(de.Document.to_json(d, indent=None))
    blob_path = f"staging/metadata-{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}.jsonl"
    storage_client.bucket(BUCKET).blob(blob_path).upload_from_string(
        "\n".join(lines) + "\n", content_type="application/json"
    )
    return f"gs://{BUCKET}/{blob_path}"


def _import_documents(gcs_uri: str) -> None:
    client = de.DocumentServiceClient()
    parent = client.branch_path(
        project=PROJECT_ID,
        location=LOCATION,
        data_store=DATA_STORE_ID,
        branch="default_branch",
    )
    op = client.import_documents(
        parent=parent,
        gcs_source=de.GcsSource(
            input_uris=[gcs_uri], data_schema="document"
        ),
        reconciliation_mode=de.ImportDocumentsRequest.ReconciliationMode.FULL,
    )
    print(f"Import op: {op.operation.name}")
    op.result()  # blocks until done


def main() -> int:
    sf = SuccessFactorsClient(SFConfig(
        api_host=SF_API_HOST,
        company_id=SF_COMPANY_ID,
        client_id=SF_CLIENT_ID,
        api_user=SF_API_USER,
        token_url=f"https://{SF_API_HOST}/oauth/token",
        private_key_pem=SF_PRIVATE_KEY,
        certificate_pem=SF_CERT,
    ))
    storage_client = storage.Client()

    since = _read_high_water_mark(storage_client)
    print(f"Last successful sync: {since or '(initial run)'}")

    docs = _build_documents(sf, storage_client, since=None)
    # Note: we always pull the full set for FULL reconciliation. The high-water
    # mark is purely for logging/observability. If your tenant is large, switch
    # to INCREMENTAL and pass `since` into _build_documents.
    if not docs:
        print("No documents found.")
        return 0

    gcs_uri = _stage_metadata_jsonl(storage_client, docs)
    print(f"Staged {len(docs)} docs at {gcs_uri}")
    _import_documents(gcs_uri)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_high_water_mark(storage_client, now)
    print(f"Done. High-water mark set to {now}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
