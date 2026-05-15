# Enterprise-scale ingestion connector

Read this only if the basic pattern in `ingestion-connector.md` won't fit. Indicators that you've outgrown the basic pattern:

- More than ~100k documents, or growing fast.
- Sub-hour freshness requirements.
- Frequent ACL-only updates that don't change content (re-ingesting is wasteful).
- Strict deletion-tracking needs (you can't just rely on weekly FULL re-syncs).
- A naive Python script tops out around 10–15 docs/sec; this pattern hits ~36k docs/sec on the BigQuery import path.

## BigQuery-backed CDC architecture

The pattern leans on BigQuery as the system of record for "what should be in the datastore right now." Discovery Engine becomes a downstream materialized view; deltas are computed in SQL, not Python.

Five tables in a `stg_*` dataset:

| Table | Purpose |
|---|---|
| `stg_source_events` | Append-only event stream from the source. Never destructive. Captures every insert / update / delete event with timestamps. |
| `stg_active_state` | Current truth, resolved from the event stream. The "what should exist" view. |
| `stg_target_mirror` | Snapshot of what's actually in the Discovery Engine datastore. Carries `last_synced` watermark per document. |
| `stg_sync_history` | Audit log of every sync operation — import counts, errors, run IDs. |
| `stg_import_batch` | Transient staging table holding the JSON payloads about to be exported to GCS. |

The sync loop:

1. Append new events to `stg_source_events`.
2. `MERGE` into `stg_active_state` to resolve to current truth.
3. Diff `stg_active_state` vs. `stg_target_mirror` → identify inserts, updates, deletes.
4. For inserts/updates: build payloads in `stg_import_batch`, export as JSONL to GCS, `import_documents` with `INCREMENTAL`.
5. For deletes: extract missing IDs, dump JSONL to GCS, `PurgeDocuments`.
6. Update `stg_target_mirror` watermarks, write run summary to `stg_sync_history`.

All deltas are SQL; the Python side is only orchestrating GCS uploads and Discovery Engine RPCs.

## Why this scales

- **No Python loops over records.** State reconciliation happens in `MERGE` statements that BigQuery parallelizes.
- **Idempotent.** Re-running a sync is a no-op when nothing changed (the diff returns empty).
- **Bounded memory.** The connector never materializes the full corpus in Python — it streams from BigQuery directly to GCS.
- **Auditable.** `stg_sync_history` makes "what changed and when" a SQL query, not a log search.

## Unstructured data — BigQuery External Object Tables

For corpora of binary files (PDFs, Office docs) in GCS, an External Object Table projects bucket metadata into BigQuery without downloading files. You can then do SQL-based delta detection on millions of files:

```sql
CREATE EXTERNAL TABLE policy_docs_meta
WITH CONNECTION `region.my-conn`
OPTIONS (
  object_metadata = 'SIMPLE',
  uris = ['gs://policy-docs/*']
);

-- "Which files changed since last sync?"
SELECT uri, updated, size
FROM policy_docs_meta
WHERE updated > (SELECT MAX(last_synced) FROM stg_target_mirror);
```

This is the only sane way to handle "is anything new in the bucket?" once you cross ~100k objects.

## The Patch trick — ACL-only updates without re-ingesting content

When only metadata or ACLs change but the document content is unchanged (common: permission grants/revokes), use `DocumentService.Patch` instead of `import_documents`. It updates the ACL in milliseconds without re-uploading the body.

```python
import concurrent.futures
from google.cloud import discoveryengine_v1 as discoveryengine
from google.protobuf import field_mask_pb2

def patch_acl(client, name, new_readers):
    return client.update_document(
        request=discoveryengine.UpdateDocumentRequest(
            document=discoveryengine.Document(
                name=name,
                acl_info=discoveryengine.Document.AclInfo(
                    readers=[discoveryengine.Document.AclInfo.AccessRestriction(
                        principals=[discoveryengine.Principal(**p) for p in new_readers],
                    )],
                ),
            ),
            update_mask=field_mask_pb2.FieldMask(paths=["acl_info"]),
        )
    )

def patch_many(updates, max_workers=10):
    client = discoveryengine.DocumentServiceClient()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(patch_acl, client, u["name"], u["readers"]) for u in updates]
        for f in concurrent.futures.as_completed(futures):
            yield f.result()
```

`max_workers=10` is the sweet spot — higher gets rate-limited.

## PurgeDocuments at scale

For batch deletes, `PurgeDocuments` accepts a filter expression and is a long-running operation (handles up to ~100k documents per request):

```python
op = client.purge_documents(
    request=discoveryengine.PurgeDocumentsRequest(
        parent=branch,
        filter='id: ANY("doc-1","doc-2",...)',
        force=True,
    )
)
op.result()  # blocks until done
```

For more than ~100k deletes, chunk the filter list.

## Custom embeddings and serving controls

The advanced features below are only useful when the user explicitly needs them — don't add them by default.

- **Custom embeddings**: map your own vectors into the `embedding_vector` schema property. Useful when the source domain has specialized terminology and you've trained an embedding model on it.
- **Filterable metadata facets**: mark fields in `struct_data` as filterable to power UI filters or agent-driven scoping.
- **Serving controls**: boost documents based on metadata (recency, source-of-truth flag) or user context (department, tenure).

## Reference implementation

The Medium article ["Building Enterprise-Scale Custom Connectors for Vertex AI Discovery Engine & Gemini Enterprise"](https://medium.com/google-cloud/building-enterprise-scale-custom-connectors-for-vertex-ai-discovery-engine-gemini-enterprise-99a136f561e7) has a full GitHub reference implementation including Cloud Run simulators for structured (ticketing) and unstructured (HTML knowledge base) sources. Borrow the table DDL and the MERGE statements rather than reinventing.
