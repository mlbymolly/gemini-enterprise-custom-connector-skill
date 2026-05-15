# Ingestion connector scaffold for Gemini Enterprise

This is the detailed reference for **Path B** in SKILL.md. Read SKILL.md first to confirm an ingestion connector is the right path.

## Architecture

```
Source (Snowflake / SAP / API)
   │
   ▼  Fetch — paginated or streamed reads
[ Connector job ]
   │
   ▼  Transform — to discoveryengine.Document
[ JSONL on Cloud Storage ]
   │
   ▼  Sync — import_documents with reconciliation_mode
[ Discovery Engine datastore (acl_enabled=True) ]
   │
   ▼  Attached to
[ Gemini Enterprise app ]
```

A connector job runs on a schedule (Cloud Scheduler → Cloud Run Job is the conventional pattern). First run is `FULL`; subsequent runs are `INCREMENTAL` with a watermark.

## Project layout

```
my-ingestion-connector/
├── pyproject.toml
├── connector.py           # Entry point — orchestrates fetch/transform/sync
├── source_fetcher.py      # Source-specific paginated reads
├── transform.py           # Source record → discoveryengine.Document
├── identity_mapping.py    # IMS setup + ACL helpers
├── infra/
│   ├── create_datastore.py    # One-time: creates IMS + datastore
│   └── service_account.tf      # IAM bindings
├── Dockerfile
└── README.md
```

## One-time: create the Identity Mapping Store

Skip this section if the source uses Google Workspace emails as identities. Run it once per project, **before** creating the datastore — the IMS reference is set at datastore creation time and can't be added later.

```python
from google.cloud import discoveryengine_v1 as discoveryengine

PROJECT_ID = "my-project"
LOCATION = "global"
IMS_ID = "my-ims"

def create_ims():
    client = discoveryengine.IdentityMappingStoreServiceClient()
    parent = f"projects/{PROJECT_ID}/locations/{LOCATION}"
    request = discoveryengine.CreateIdentityMappingStoreRequest(
        parent=parent,
        identity_mapping_store_id=IMS_ID,
        identity_mapping_store=discoveryengine.IdentityMappingStore(),
    )
    return client.create_identity_mapping_store(request=request)
```

Then load mappings (external identity → Google subject):

```python
def import_identity_mappings(entries):
    client = discoveryengine.IdentityMappingStoreServiceClient()
    name = f"projects/{PROJECT_ID}/locations/{LOCATION}/identityMappingStores/{IMS_ID}"
    inline_source = discoveryengine.ImportIdentityMappingsRequest.InlineSource(
        identity_mapping_entries=[
            discoveryengine.IdentityMappingEntry(
                external_identity=e["external"],   # e.g. "wp-admins" (NO prefix here)
                user_id=e.get("user_id"),          # OR group_id=...
                group_id=e.get("group_id"),
            )
            for e in entries
        ]
    )
    op = client.import_identity_mappings(
        request=discoveryengine.ImportIdentityMappingsRequest(
            identity_mapping_store=name,
            inline_source=inline_source,
        )
    )
    return op.result()
```

Asymmetry to remember: when *importing* mappings, the external identity is bare (`"wp-admins"`). When *referencing* the same identity in a document ACL, you prefix it (`"external_group:wp-admins"` for groups, `"external_user:..."` for users). The IMS resolves this at query time.

## One-time: create the datastore

```python
DATASTORE_ID = "my-datastore"

def create_datastore():
    client = discoveryengine.DataStoreServiceClient()
    parent = client.collection_path(PROJECT_ID, LOCATION, "default_collection")
    ims_name = f"projects/{PROJECT_ID}/locations/{LOCATION}/identityMappingStores/{IMS_ID}"
    op = client.create_data_store(
        request=discoveryengine.CreateDataStoreRequest(
            parent=parent,
            data_store_id=DATASTORE_ID,
            data_store=discoveryengine.DataStore(
                display_name="My custom datastore",
                acl_enabled=True,                      # MUST be set at creation
                industry_vertical=discoveryengine.IndustryVertical.GENERIC,
                identity_mapping_store=ims_name,       # omit if not using IMS
                solution_types=[
                    discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH,
                ],
            ),
        )
    )
    return op.result()
```

`acl_enabled=True` cannot be added later. If you forget, you have to delete and recreate the datastore — and re-import all documents.

## Transform — building Document payloads

Every record from the source becomes one `Document`. Stable IDs are non-negotiable.

```python
from google.cloud import discoveryengine_v1 as discoveryengine
import json

def to_document(record) -> discoveryengine.Document:
    # Derive a STABLE id from the source primary key.
    doc_id = f"sap_hr:{record['employee_id']}:{record['doc_kind']}"

    # Body — UTF-8 text or extracted document text.
    body_text = record["body"]

    # Metadata you want filterable/facetable.
    struct = {
        "title": record["title"],
        "source_url": record["url"],
        "owner": record["owner_email"],
        "tags": record.get("tags", []),
        "updated_at": record["updated_at"].isoformat(),
    }

    # ACL — who can read this document.
    readers = []
    for email in record.get("reader_emails", []):
        readers.append({"user_id": email})
    for group in record.get("reader_groups", []):
        # If group uses external identity, reference it with the prefix.
        readers.append({"group_id": f"external_group:{group}"})
    if record.get("public"):
        readers = [{"idp_wide": True}]

    return discoveryengine.Document(
        id=doc_id,
        struct_data=struct,
        content=discoveryengine.Document.Content(
            raw_bytes=body_text.encode("utf-8"),
            mime_type="text/plain",
        ),
        acl_info=discoveryengine.Document.AclInfo(
            readers=[discoveryengine.Document.AclInfo.AccessRestriction(
                principals=[discoveryengine.Principal(**p) for p in readers],
            )],
        ),
    )
```

For binary content (PDFs, images, Office docs), use the file's bytes as `raw_bytes` with the correct `mime_type` — Discovery Engine extracts text server-side.

## Sync method A: inline import (development)

Fine for the first run while you're debugging mappings. Inline imports are **incremental-only** — no clean replacement.

```python
def import_inline(documents):
    client = discoveryengine.DocumentServiceClient()
    parent = client.branch_path(
        project=PROJECT_ID, location=LOCATION,
        data_store=DATASTORE_ID, branch="default_branch",
    )
    op = client.import_documents(
        request=discoveryengine.ImportDocumentsRequest(
            parent=parent,
            inline_source=discoveryengine.ImportDocumentsRequest.InlineSource(
                documents=documents,
            ),
            # Inline only supports INCREMENTAL.
        )
    )
    return op.metadata
```

Inline imports cap at 100 documents per request — chunk if you have more.

## Sync method B: GCS JSONL import (production)

The path you want for anything real. Supports `FULL` (clean replacement) and `INCREMENTAL` reconciliation, and scales far higher than inline.

```python
from google.cloud import storage

def write_jsonl_to_gcs(documents, bucket, blob_name):
    jsonl = "\n".join(
        discoveryengine.Document.to_json(d, indent=None) for d in documents
    ) + "\n"
    storage.Client().bucket(bucket).blob(blob_name).upload_from_string(
        jsonl, content_type="application/json",
    )
    return f"gs://{bucket}/{blob_name}"

def import_from_gcs(gcs_uri, mode="INCREMENTAL"):
    client = discoveryengine.DocumentServiceClient()
    parent = client.branch_path(
        project=PROJECT_ID, location=LOCATION,
        data_store=DATASTORE_ID, branch="default_branch",
    )
    op = client.import_documents(
        request=discoveryengine.ImportDocumentsRequest(
            parent=parent,
            gcs_source=discoveryengine.GcsSource(input_uris=[gcs_uri]),
            reconciliation_mode=(
                discoveryengine.ImportDocumentsRequest.ReconciliationMode.FULL
                if mode == "FULL"
                else discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL
            ),
        )
    )
    return op  # Long-running; poll op.done() / op.result()
```

GCS limits to design around:
- Max 100 files per request — or 100,000 if `dataSchema=content`.
- Per-file: 2GB, or 100MB if `dataSchema=content`.
- For 1M+ docs, split into multiple files (one per shard) and submit them together in one request.

## Sync method C: PurgeDocuments (handle deletes)

`INCREMENTAL` imports never delete. To remove records that disappeared from the source:

```python
def purge_documents(document_ids):
    client = discoveryengine.DocumentServiceClient()
    parent = client.branch_path(
        project=PROJECT_ID, location=LOCATION,
        data_store=DATASTORE_ID, branch="default_branch",
    )
    # Filter syntax: '*' purges everything matching the filter expression.
    op = client.purge_documents(
        request=discoveryengine.PurgeDocumentsRequest(
            parent=parent,
            filter="*",   # or e.g. f"id: ANY({','.join(document_ids)})"
            force=True,
        )
    )
    return op.result()
```

Alternatively: do a `FULL` re-sync periodically. Cheaper to reason about, expensive at scale.

## Scheduling

Run the connector on Cloud Run Jobs, triggered by Cloud Scheduler. A typical cadence:

- **Initial run**: `FULL` import of every record.
- **Incremental runs**: every 15 min – 1 hour, query `WHERE updated_at > {watermark}`, import incremental, advance watermark.
- **Weekly reconciliation**: `FULL` import to catch drift and deletes.

Persist the watermark somewhere durable (Firestore, Cloud SQL, or even GCS) — losing it means you re-ingest everything, which is annoying but recoverable.

## Service account permissions

The connector service account needs:

- `roles/discoveryengine.editor` on the project (or finer-grained `dataStores.*` and `documents.*` permissions)
- `roles/storage.objectAdmin` on the GCS staging bucket
- Whatever the source needs (Snowflake user/role, SAP credentials, API key)

If the connector runs on Cloud Run, use workload identity rather than a service account key.

## Attaching to a Gemini Enterprise app

1. **Gemini Enterprise → Apps → Create app** (or edit an existing one).
2. **Data stores → Add** → pick the custom datastore you created.
3. Save. Queries to the app now ground in your datastore. ACLs are enforced at query time using the asking user's identity (resolved through the IMS if external).

## Common failure modes

- **No results in queries**: ACL mismatch. Either no `readers` set (default-deny), or the asking user doesn't map to any principal in the readers list. Test with `idp_wide: true` on a single document to confirm indexing worked.
- **Duplicates after re-sync**: document IDs aren't stable (e.g., generated with `uuid4()` each run). Derive IDs from source primary keys.
- **`acl_enabled` errors when querying**: datastore created without `acl_enabled=True`. Recreate.
- **`identity_mapping_store` not found**: wrong format. Must be `projects/{p}/locations/{l}/identityMappingStores/{ims_id}`, not just the ID.
- **Inline import "too many documents"**: cap is 100 per call. Chunk.
- **FULL import times out / hits limits**: split into multiple JSONL files (one per source shard) and submit in one request with multiple `input_uris`.
