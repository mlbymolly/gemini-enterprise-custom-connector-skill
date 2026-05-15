# Scaffold: Internal CMS articles → Gemini Enterprise datastore

## Path decision

Walking the decision tree from `SKILL.md`:

1. **Snapshot vs. current state?** Hourly updates is a snapshot — you don't need read-time freshness against the live DB.
2. **Need to write back?** No, this is read-only ingestion of articles.
3. **Want grounded, cited answers searchable alongside other corpora?** Articles in a CMS are exactly that use case.

→ **Path B: Custom Connector → Discovery Engine datastore.**

The volume (~50k docs) is comfortably under the ~100k threshold at which `SKILL.md` redirects you to the BigQuery-backed CDC pattern in `references/enterprise-scale.md`. The straightforward "fetch → transform → JSONL on GCS → import_documents" pipeline from `references/ingestion-connector.md` fits, with headroom to grow toward 100k before you need to redesign.

## Assumptions (call these out and confirm)

1. **CMS user identities are NOT Google Workspace identities.** "Internal CMS" implies internal usernames/group names that Discovery Engine can't resolve directly, so we provision an **Identity Mapping Store**. If everyone in the CMS already authenticates as their Workspace email, you can drop the IMS and use `user_id`/`group_id` directly — but the safer default for an internal CMS is to keep it.
2. **The CMS REST API supports `updated_since` filtering and cursor pagination.** If it doesn't, `source_fetcher.py` is the only file that needs to change (you'd page by `id` or `offset` and filter `updated_at` client-side). The rest of the pipeline is source-agnostic.
3. **The article body is HTML** (typical for CMS articles), so `transform.py` strips tags before ingest. If you want richer extraction (preserving headings/lists/tables), swap the regex stripper for `trafilatura` or `BeautifulSoup`. If the body is already markdown or plain text, remove the stripper entirely.
4. **Per-article ACLs come from the CMS** as `reader_users`, `reader_groups`, and an `is_public` flag. If your CMS doesn't expose ACLs per article (e.g., everyone with a CMS account can read everything), set `is_public=True` for all records and skip the IMS entirely.
5. **Hourly cadence + no sub-hour SLA + no need for ACL-only patches** keeps you out of `enterprise-scale.md` territory. If any of that changes (frequent permission churn, sub-hour freshness, or growth past ~100k), graduate to the BigQuery CDC pattern.
6. **Runs on Cloud Run Jobs**, triggered by Cloud Scheduler. Workload Identity (no service-account keys).

## Architecture

```
api.internal/articles  (Postgres-backed REST, cursor-paginated)
        │
        ▼  fetch_articles(since=watermark)        [source_fetcher.py]
   articles iterator
        │
        ▼  to_document(record)                    [transform.py]
   discoveryengine.Document iterator
        │
        ▼  shard + write JSONL                    [connector.py]
   gs://staging/imports/<run_id>/shard-*.jsonl
        │
        ▼  import_documents(GcsSource, mode)
   Discovery Engine datastore (acl_enabled=True, IMS bound)
        │
        ▼  attached to
   Gemini Enterprise app
```

Sync cadence:

- **First run** (no watermark): `FULL` reconciliation. All ~50k articles. Expect 5 shards of 10k each — well under the 100-files-per-request limit.
- **Hourly runs**: `INCREMENTAL` with `WHERE updated_at > watermark`. Typically tens to hundreds of records.
- **Weekly recommended**: re-run as `FULL` to catch drift and deletes. (Hard deletes between FULLs require `PurgeDocuments` — see the deletes section below.)

## Project layout

Generated under `outputs/`:

```
outputs/
├── pyproject.toml
├── Dockerfile
├── connector.py            # Orchestrator (fetch → transform → JSONL → import)
├── source_fetcher.py       # CMS REST pagination + retry
├── transform.py            # record → discoveryengine.Document; ACL mapping
├── identity_mapping.py     # IMS bootstrap + mapping import
└── infra/
    └── create_datastore.py # One-time: creates IMS + datastore (acl_enabled=True)
```

## Bring-up sequence

These are one-time, in order. They mirror the scaffold checklist in `SKILL.md` Path B.

1. **Enable APIs** in the GCP project: `discoveryengine.googleapis.com`, `storage.googleapis.com`, `run.googleapis.com`, `cloudscheduler.googleapis.com`.
2. **Provision the staging GCS bucket** (single-region, near your Discovery Engine location). Name it something like `gs://<proj>-cms-connector-staging`.
3. **Create a service account** for the connector with:
   - `roles/discoveryengine.editor` on the project
   - `roles/storage.objectAdmin` on the staging bucket
4. **Create the IMS and the datastore** (must happen before the first run, because `acl_enabled=True` and the IMS binding are creation-time-only):
   ```bash
   export PROJECT_ID=...
   export LOCATION=global
   export DATASTORE_ID=internal-cms-articles
   export IMS_ID=internal-cms-ims
   python infra/create_datastore.py
   ```
5. **Seed identity mappings.** Run a one-off (or schedule as a sibling job) that reads your IdP / HR feed and calls `identity_mapping.sync_mappings(...)` with the bare external identity → Google subject pairs. The asymmetry to keep in mind (from `references/ingestion-connector.md`): IMS imports take the bare name (`"engineering"`); ACLs on documents prefix it (`"external_group:engineering"`).
6. **Build & push the container, deploy as a Cloud Run Job**:
   ```bash
   gcloud builds submit --tag gcr.io/$PROJECT_ID/cms-connector
   gcloud run jobs create cms-connector \
     --image gcr.io/$PROJECT_ID/cms-connector \
     --region us-central1 \
     --service-account cms-connector@$PROJECT_ID.iam.gserviceaccount.com \
     --set-env-vars PROJECT_ID=$PROJECT_ID,LOCATION=$LOCATION,\
   DATASTORE_ID=$DATASTORE_ID,STAGING_BUCKET=$PROJECT_ID-cms-connector-staging,\
   CMS_BASE_URL=https://api.internal/articles \
     --set-secrets CMS_API_KEY=cms-api-key:latest
   ```
7. **First run** (FULL): `gcloud run jobs execute cms-connector`. With no watermark file present, `connector.py` does a full sync. Validate a few documents in the Cloud Console; spot-check ACLs by querying as a test user.
8. **Schedule hourly runs**:
   ```bash
   gcloud scheduler jobs create http cms-connector-hourly \
     --schedule "0 * * * *" \
     --uri "https://<region>-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/cms-connector:run" \
     --http-method POST \
     --oauth-service-account-email cms-scheduler@$PROJECT_ID.iam.gserviceaccount.com
   ```
9. **Attach the datastore to a Gemini Enterprise app**: Gemini Enterprise → Apps → (your app) → Data stores → Add → pick `internal-cms-articles`. Save.

## Key design points the scaffold gets right

- **Stable document IDs** derived from the CMS primary key: `f"cms_article:{record['id']}"`. Per `SKILL.md` "Common pitfalls", random UUIDs would create duplicates on every sync.
- **`acl_enabled=True` set at datastore creation** (`infra/create_datastore.py`). Cannot be added later — forgetting forces a delete + full re-ingest.
- **IMS bound at creation** with the full resource name `projects/.../identityMappingStores/...`, not just the ID.
- **GCS JSONL imports, not inline.** Inline is incremental-only and caps at 100 docs/request — useless at 50k. GCS path supports both `FULL` and `INCREMENTAL`, and lets us shard.
- **Sharding within the 100-files-per-request limit.** `SHARD_SIZE=10000` → 5 files for 50k; ~50 files even if it grew 10×. The connector errors out loudly if you ever exceed 100 shards (signal to split into multiple import requests).
- **Watermark in GCS** at `gs://<bucket>/state/watermark.txt`. Advanced only after a successful import — failures keep the old watermark so the next run re-processes the same window (idempotent because IDs are stable).
- **Default-deny ACL sentinel.** If a record arrives with no readers and `is_public=False`, we attach an unmappable sentinel principal rather than an empty readers list, so the doc is visibly default-denied in the index instead of behaving like "no ACL set."

## Handling deletes (not in the hourly run)

`INCREMENTAL` never removes documents. Two options:

1. **Weekly `FULL` re-sync** — simplest. Set `FORCE_FULL=true` in a second Scheduler job once a week; have `connector.py` honor that env var and force `mode = "FULL"`. The full re-sync replaces the entire branch, so anything missing from the source is dropped.
2. **`PurgeDocuments` on a delete feed** — only if your CMS publishes hard deletes. Query the CMS for IDs deleted since the last sync, then call `client.purge_documents(filter='id: ANY("cms_article:123",...)', force=True)`. Snippet is in `references/ingestion-connector.md`.

For 50k articles, weekly FULL is fine and dramatically simpler. Revisit if FULLs start taking too long or deletes need to propagate faster than weekly.

## When to graduate to the enterprise-scale pattern

Promote to the BigQuery CDC pattern in `references/enterprise-scale.md` when any of these become true:

- Article count crosses ~100k (Python iteration starts to bottleneck around 10–15 docs/sec).
- Freshness SLA tightens below an hour.
- Permission churn becomes frequent — the Patch API (ACL-only updates without re-uploading content) is the killer feature there.
- You need an auditable "what changed when" trail.

Until then, this scaffold is the right size.

## Files generated

All under `/Users/Molly.Depew/custom-connectors/gemini-enterprise-custom-connector-workspace/iteration-1/eval-2-internal-cms-articles/with_skill/outputs/`:

- `connector.py` — orchestrator
- `source_fetcher.py` — paginated CMS reads with retry
- `transform.py` — record → Document mapping, ACL handling
- `identity_mapping.py` — IMS bootstrap + mapping import
- `infra/create_datastore.py` — one-time IMS + datastore creation
- `pyproject.toml`
- `Dockerfile`

## Open questions to confirm before first run

1. Are CMS user/group identities Workspace-aligned, or do we really need the IMS? (Drives whether you run `infra/create_datastore.py` with or without the IMS binding.)
2. Does the CMS API actually support `updated_since` + cursor pagination as assumed? (If not, `source_fetcher.py` needs adjustment — nothing else changes.)
3. Are article bodies HTML, markdown, or plain text? (Affects whether `transform.py` keeps the HTML stripper.)
4. Is `global` the right Discovery Engine location, or do you need regional (`us`, `eu`) for data-residency reasons? (Set once, hard to change.)
5. Where do permission-changes happen — in the CMS itself, or in a separate IdP feed? (Determines whether the same hourly job is enough or whether you want a faster ACL-patch job, which is the cue to look at `enterprise-scale.md`.)
