# Databricks ingestion connector

Fetch → Transform → Sync pipeline that pushes Databricks content into a Discovery Engine datastore for Gemini Enterprise to ground on.

Two source types are supported in the same job:

- **Unity Catalog volume files** — PDFs, markdown, Office docs, etc. One Document per file. Discovery Engine extracts text server-side from binary formats.
- **Unity Catalog table rows** — small reference tables. One Document per row, body is a rendered prose string ("col1: value1. col2: value2."). Don't use this for transactional/event tables — that's what the MCP server is for.

## One-time setup

```bash
# Enable the API
gcloud services enable discoveryengine.googleapis.com

# Create the IMS (skip if all readers are Google Workspace emails) and the datastore
python infra/create_datastore.py \
    --project my-gcp-project \
    --location global \
    --ims-id databricks-ims \
    --datastore-id databricks-docs

# Map Databricks groups → Google subjects. See "Identity mappings" below.
python infra/import_identities.py \
    --project my-gcp-project \
    --location global \
    --ims-id databricks-ims \
    --config infra/mappings.json
```

## Identity mappings

The transforms in `transform.py` write document ACLs that reference Databricks groups via the `external_group:` prefix. Those references are resolved at query time by the Identity Mapping Store (IMS). Until you load mappings, ACLs reference identities Discovery Engine can't resolve — and the document is treated as unreadable.

`infra/import_identities.py` reads a JSON config that names each Databricks group and tells the script how to translate it:

- `google_group: "data-eng@example.com"` — 1:1 mapping. Use when there's a Google Workspace group whose membership matches the Databricks group.
- `expand_members: true` — pulls current members from Databricks via SCIM and creates one mapping per member. Use when there's no Google group analog, or when you want point-in-time membership.

See `infra/mappings.example.json` for the full schema. The script reads `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET` from the environment (same SP as the ingestion job).

### Keeping mappings fresh

`expand_members` doesn't auto-remove people who left a Databricks group — old entries linger and continue granting access. Two ways to handle it:

1. **Re-purge on every run** (`--repurge-expanded`): script calls `purge_identity_mappings` for each expanded group before re-importing. Simple and correct. Brief window during the import where access is degraded.
2. **Diff-based** (advanced): list current IMS entries, compare to current Databricks membership, only purge the deltas. More code, no access gap.

Default to #1 unless you have a quiet window problem. Run the import on the same schedule as the ingestion job — once an hour is plenty for most orgs.

### Deploy as a separate Cloud Run Job

The identity importer ships in the same container image as the document connector but runs as its own job. Keeping them separate means a flaky identity push can't block a document sync (and vice versa), and the two cadences can diverge — most teams want hourly docs but only need 4×/day identity refreshes.

```bash
# Reuse the image built for the document connector; override the entrypoint.
gcloud run jobs deploy databricks-identity-import \
    --image us-central1-docker.pkg.dev/my-gcp-project/connectors/databricks-ingestion:latest \
    --region us-central1 \
    --service-account ingestion-sa@my-gcp-project.iam.gserviceaccount.com \
    --command "python" \
    --args "infra/import_identities.py,\
--project,my-gcp-project,\
--location,global,\
--ims-id,databricks-ims,\
--config,infra/mappings.json,\
--repurge-expanded" \
    --set-secrets "DATABRICKS_CLIENT_ID=databricks-sp-client-id:latest,\
DATABRICKS_CLIENT_SECRET=databricks-sp-client-secret:latest" \
    --set-env-vars "DATABRICKS_HOST=dbc-xxxx.cloud.databricks.com"

# 4×/day refresh
gcloud scheduler jobs create http databricks-identity-import-6h \
    --schedule "0 */6 * * *" \
    --uri "https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/my-gcp-project/jobs/databricks-identity-import:run" \
    --http-method POST \
    --oauth-service-account-email scheduler-sa@my-gcp-project.iam.gserviceaccount.com
```

The mappings JSON itself lives outside the image — either bake it into the image at build time (simplest, redeploy on changes) or mount it from GCS via a startup script. Don't put it in Secret Manager; it isn't a secret and Secret Manager isn't a great fit for files.

### Dry-run before pushing

```bash
python infra/import_identities.py \
    --project my-gcp-project \
    --location global \
    --ims-id databricks-ims \
    --config infra/mappings.json \
    --dry-run
```

Prints the first 20 mappings without calling the IMS. Use this to confirm the config is right before a real import.

### The bare-vs-prefixed asymmetry (read this once)

When you *import* a group mapping, the external identity is bare:

```python
Mapping(external_identity="data-engineers", google_group="data-eng@example.com")
```

When `transform.py` references that same group in a document ACL, it prefixes it:

```python
discoveryengine.Principal(group_id="external_group:data-engineers")
```

The IMS joins them at query time. Both helper modules already handle this — but if you hand-roll an ACL or a mapping, follow the asymmetry or nothing matches.

If `acl_enabled=True` isn't set at creation, you'll have to delete and recreate the datastore — there's no patch path. The script above sets it.

## Environment variables

| Variable | Purpose |
|---|---|
| `DATABRICKS_HOST` | Workspace hostname |
| `DATABRICKS_CLIENT_ID` | Service principal OAuth client ID |
| `DATABRICKS_CLIENT_SECRET` | Service principal OAuth client secret |
| `GCP_PROJECT` | Defaults to `GOOGLE_CLOUD_PROJECT` if unset |
| `DISCOVERY_ENGINE_LOCATION` | Defaults to `global` |

## Run modes

| Mode | Reconciliation | Watermark | When to use |
|---|---|---|---|
| `initial` | `FULL` | ignored | First run, or full reset |
| `incremental` | `INCREMENTAL` | applied | Every scheduled run |
| `reconcile` | `FULL` | ignored | Weekly drift catcher; also handles deletes |

## Example: volume of policy PDFs

```bash
python connector.py \
    --mode initial \
    --watermark-key policies \
    --volume-root /Volumes/main/docs/policies \
    --gcs-staging gs://my-staging/databricks-policies \
    --project my-gcp-project \
    --datastore-id databricks-docs \
    --default-reader-group databricks_policy_readers
```

## Example: reference table

```bash
python connector.py \
    --mode incremental \
    --watermark-key product-catalog \
    --gcs-staging gs://my-staging/product-catalog \
    --project my-gcp-project \
    --datastore-id databricks-docs \
    --http-path /sql/1.0/warehouses/abcdef1234567890 \
    --table-catalog main \
    --table-schema sales \
    --table-name products \
    --table-pk product_id \
    --text-columns name description category \
    --metadata-columns price brand stock_status \
    --watermark-column updated_at \
    --default-reader-group sales_org
```

## Deploy as a Cloud Run Job

```bash
# Build & deploy the job
gcloud run jobs deploy databricks-ingestion \
    --source . \
    --region us-central1 \
    --service-account ingestion-sa@my-gcp-project.iam.gserviceaccount.com \
    --set-secrets "DATABRICKS_CLIENT_ID=databricks-sp-client-id:latest,\
DATABRICKS_CLIENT_SECRET=databricks-sp-client-secret:latest" \
    --set-env-vars "DATABRICKS_HOST=dbc-xxxx.cloud.databricks.com,\
GCP_PROJECT=my-gcp-project" \
    --args "--mode,incremental,--watermark-key,policies,\
--volume-root,/Volumes/main/docs/policies,\
--gcs-staging,gs://my-staging/databricks-policies,\
--datastore-id,databricks-docs,\
--default-reader-group,databricks_policy_readers"

# Initial run (manual, with --mode initial overriding the default args)
gcloud run jobs execute databricks-ingestion \
    --args "--mode,initial,--watermark-key,policies,..."

# Hourly schedule for incremental
gcloud scheduler jobs create http databricks-ingestion-hourly \
    --schedule "0 * * * *" \
    --uri "https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/my-gcp-project/jobs/databricks-ingestion:run" \
    --http-method POST \
    --oauth-service-account-email scheduler-sa@my-gcp-project.iam.gserviceaccount.com

# Weekly FULL reconcile
gcloud scheduler jobs create http databricks-ingestion-weekly \
    --schedule "0 3 * * 0" \
    --uri "..." \
    --message-body '{"overrides": {"containerOverrides": [{"args": ["--mode","reconcile",...]}]}}'
```

## Service account permissions

The job service account needs:

- `roles/discoveryengine.editor` on the project (or fine-grained `dataStores.*` + `documents.*`)
- `roles/storage.objectAdmin` on the staging bucket
- `roles/datastore.user` for Firestore watermark reads/writes
- Secret Manager access to the Databricks SP secrets
- On the Databricks side: `CAN USE` on the SQL Warehouse, `SELECT` on relevant schemas/tables, `READ VOLUME` on the volume

## Scale ceiling

The single-script architecture scales to roughly **10–15 docs/sec** end-to-end. That's fine for ≤ ~100k docs and hour-level freshness. Past that, switch to the BigQuery-backed CDC pattern (`references/enterprise-scale.md` in the skill): External Object Tables for volumes, `MERGE` for delta detection, Patch API for metadata-only updates. That pattern benchmarks ~36k docs/sec.

## Failure-mode quick reference

- **No results in queries after ingest** → ACL mismatch. Either no `readers` set, or the user doesn't map. Test with one `idp_wide:True` document to isolate.
- **Duplicates after re-sync** → document IDs aren't stable. The transforms here derive IDs from `volume_path` and `catalog.schema.table:pk`. Don't introduce `uuid4()`.
- **`acl_enabled` errors at query time** → datastore created without `acl_enabled=True`. Recreate via `infra/create_datastore.py`.
- **`identity_mapping_store` not found** → must be the full resource name, not just the ID. `create_datastore.py` builds the full name.
- **`import_documents` 100-file error** → the `write_jsonl_shards` + `GCS_FILE_CAP` chunking handles this; if you see it, you're calling `import_documents` directly somewhere instead of going through `import_from_gcs`.
- **Watermark didn't advance** → job crashed before `put_watermark`. The next run will redo the window — `INCREMENTAL` is idempotent on stable IDs, so this is safe.
