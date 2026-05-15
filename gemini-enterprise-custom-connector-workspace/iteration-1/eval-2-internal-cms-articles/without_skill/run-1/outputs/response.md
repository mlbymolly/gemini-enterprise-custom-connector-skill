# Custom Connector: Internal CMS → Gemini Enterprise

This scaffold ingests ~50k articles from your internal CMS REST API
(`https://api.internal/articles`) into a Gemini Enterprise (Vertex AI Search /
Discovery Engine) datastore, with **hourly incremental** updates and a
**one-shot full backfill** path.

## Assumptions

These are reasonable defaults; flip any of them before production.

1. **CMS API shape.** A paginated JSON endpoint:
   `GET /articles?page=N&page_size=K&updated_since=ISO8601`, response
   `{ items: [...], next_page, total }`. Each item has `id`, `title`,
   `body_html`, `author`, `tags`, `url`, `published_at`, `updated_at`, `status`
   (`published` / `draft` / `deleted`). The endpoint must support
   `updated_since` filtering — without that the only correct approach is a
   nightly full crawl, which is fine for 50k docs but wastes API quota.
2. **Auth.** Bearer token on the CMS side; **Application Default Credentials**
   (Cloud Run service account) on the Google side. CMS token comes from
   Secret Manager (`cms-api-token`).
3. **Datastore.** A pre-created Gemini Enterprise datastore named
   `cms-articles` in the `global` location, configured for **unstructured
   data with metadata**. Branch: `default_branch`.
4. **Volume.** ~50k articles, hourly delta well under 5k items. Inline
   `importDocuments` calls (batches of 100) are sufficient; we don't need a
   GCS staging pipeline. For the initial full-backfill, switching to a GCS
   JSONL source is recommended (note in "Scaling" below).
5. **Runtime.** Cloud Run Job triggered by Cloud Scheduler at `0 * * * *`
   UTC. Stateless container; watermark persisted to GCS.
6. **Deletes.** CMS publishes a tombstone (`status: "deleted"`) inside the
   normal delta feed. If your CMS hard-deletes silently, you need a periodic
   reconciliation job (see "Open items").

## Architecture

```
                       (hourly trigger)
Cloud Scheduler ──▶ Cloud Run Job (this container)
                          │
                          │ 1. read watermark
                          ▼
                     GCS state.json
                          │
                          │ 2. GET /articles?updated_since=...
                          ▼
                     Internal CMS API
                          │
                          │ 3. transform + batch
                          ▼
              Discovery Engine importDocuments
                  (INCREMENTAL, inline source)
                          │
                          │ 4. advance watermark
                          ▼
                     GCS state.json
```

Flow stages:
1. **Watermark read** — `connector/state.py` loads the last successful
   `max(updated_at)` from `gs://…/state.json`. Empty on first run.
2. **Extract** — `connector/cms_client.py` paginates the CMS with
   retry/backoff on 429/5xx.
3. **Transform** — `connector/transform.py` maps each row to a Discovery
   Engine `Document` (HTML body → base64 `content.rawBytes`, metadata →
   `structData`). Drafts and empty bodies are dropped; `status=deleted` is
   routed to delete calls.
4. **Load** — `connector/gemini_sink.py` calls `importDocuments` with
   `ReconciliationMode.INCREMENTAL` so existing IDs are upserted. Deletes go
   through `deleteDocument`.
5. **Checkpoint** — only after a clean run we advance the watermark to the
   largest `updated_at` observed.

## File layout

```
outputs/
├── response.md                        ← this document
├── Dockerfile
├── requirements.txt
├── connector/
│   ├── __init__.py
│   ├── config.py                      ← env-var configuration
│   ├── cms_client.py                  ← paginated REST extractor
│   ├── transform.py                   ← CMS row → DE Document
│   ├── gemini_sink.py                 ← Discovery Engine writer
│   ├── state.py                       ← watermark in GCS / local JSON
│   └── main.py                        ← entrypoint
├── deploy/
│   └── cloud_run_job.sh               ← build + deploy + schedule
└── tests/
    └── test_transform.py
```

## How to run

### Local smoke test

```bash
pip install -r requirements.txt
export CMS_BASE_URL=https://api.internal/articles
export CMS_API_TOKEN=...                  # bearer token
export GCP_PROJECT_ID=my-project
export DATASTORE_ID=cms-articles
export STATE_URI=./state.json             # local file for dev
export RUN_MODE=incremental
python -m connector.main
```

### Initial full backfill

```bash
RUN_MODE=full python -m connector.main
```

For real 50k backfill, prefer GCS staging (see "Scaling").

### Deploy

```bash
PROJECT_ID=my-project REGION=us-central1 ./deploy/cloud_run_job.sh
```

This builds the image, deploys the Cloud Run Job, and registers an hourly
Cloud Scheduler trigger. The job uses a dedicated service account with:
- `roles/discoveryengine.editor` on the datastore,
- `roles/storage.objectAdmin` on the state bucket,
- `roles/secretmanager.secretAccessor` on `cms-api-token`.

### Tests

```bash
pytest tests/
```

## Scaling notes

- **Hourly delta (~few thousand rows):** the current inline `importDocuments`
  path is fine. Each batch is 100 docs; ~50 calls/hour worst case.
- **Initial backfill (50k):** switch to GCS staging. Stream rows as JSONL
  to `gs://…/backfill/articles-*.jsonl`, then call `importDocuments` with a
  `gcs_source` instead of `inline_source`. One LRO, ~minutes, no client-side
  batching loop. Easy add to `gemini_sink.py`.
- **Concurrency:** `CONFIG.max_workers` is plumbed in for the requests
  session pool size. Today the loop is sequential — the CMS rate limit will
  almost always be the bottleneck, not Discovery Engine.
- **Cost.** Discovery Engine charges per document and per query. 50k docs is
  trivial; the cost driver will be query volume on the search side.

## Failure modes & how this scaffold handles them

| Failure | Behavior |
|---|---|
| CMS 5xx / 429 | `urllib3.Retry` with exponential backoff, honors `Retry-After` |
| CMS auth fail (401/403) | Raises, job fails, **watermark not advanced** — next hour retries the same window |
| Discovery Engine transient | `google.api_core.retry` on the sink |
| Bad row (empty body / draft) | Logged + skipped, run still succeeds |
| Job killed mid-run | Watermark only advances at the end; next run re-reads since the last good watermark. Discovery Engine `INCREMENTAL` mode is upsert-idempotent so re-sending the same doc is safe |
| Mid-run partial batches | We flush remainders before advancing the watermark |

## Open items / things to confirm before production

1. **Hard deletes.** If the CMS doesn't emit `status=deleted` tombstones, add
   a weekly reconciliation that lists all current CMS IDs vs.
   `listDocuments()` and deletes the difference.
2. **Schema.** If you want filterable facets (e.g., search by `author` or
   `tags`), define a custom Discovery Engine schema with those fields marked
   `indexable` / `retrievable` / `filterable`, and replace
   `schemaId="default_schema"` accordingly.
3. **PII / access control.** If articles have per-user ACLs, populate
   `aclInfo` on the Document — Gemini Enterprise will then filter results
   per caller identity.
4. **Clock skew.** The watermark uses the CMS's `updated_at`. If multiple
   CMS replicas have skewed clocks, subtract a small safety window (e.g.,
   `updated_since = max_updated_at - 60s`) and rely on idempotent upserts to
   absorb the overlap.
5. **Observability.** Wire structured logs to Cloud Logging (already
   stdout-friendly) and create an alert on Cloud Run Job failures + on
   "watermark age > 3 hours".

## Key references

- Discovery Engine `importDocuments` (Python client):
  `google.cloud.discoveryengine_v1.DocumentServiceClient.import_documents`
- Reconciliation modes: `INCREMENTAL` (upsert) vs `FULL` (replace branch).
  This connector deliberately uses `INCREMENTAL` to avoid re-indexing 50k
  docs every hour.
- Cloud Run Jobs + Scheduler is the recommended pattern for hourly batch
  ingestion when the workload is < ~1h and stateless.
