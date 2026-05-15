# Setup guide — Databricks ↔ Gemini Enterprise connectors

End-to-end checklist for standing up both subprojects (`mcp-server/` for live SQL and `ingestion-connector/` for indexed docs).

Follow the sections in order. Items marked **[once]** are project-wide one-time setup; **[per env]** repeats per environment (dev/prod); **[per source]** repeats per volume or table you want indexed.

---

## 0. Values to gather first

Fill these in before you start clicking. You'll need every one of them.

### GCP
| Value | Where to find / set | Example |
|---|---|---|
| Project ID | GCP console | `acme-gemini-prod` |
| Region | Pick one; everything below stays in this region | `us-central1` |
| Discovery Engine location | `global` is the default; only change if you have data-residency reasons | `global` |
| GCS staging bucket name | Create new; needs `roles/storage.objectAdmin` for the ingestion SA | `acme-gemini-staging` |
| Firestore database | Native mode, single region | `(default)` |

### Databricks
| Value | Where to find / set | Example |
|---|---|---|
| Workspace hostname | Browser URL of the workspace, no scheme | `dbc-12345678-abcd.cloud.databricks.com` |
| SQL Warehouse HTTP path | Warehouse → Connection details | `/sql/1.0/warehouses/abcdef1234567890` |
| Service principal app ID | Account console → Service principals | `00000000-0000-0000-0000-000000000000` |
| Service principal client secret | Generated when you create the OAuth secret | `dose-...` |
| Unity Catalog volume root(s) | UC explorer | `/Volumes/main/docs/policies` |
| Reference table(s) to index | UC explorer | `main.sales.products` |
| Databricks group → Google group mappings | Hand-authored | see `infra/mappings.example.json` |

### Customer IdP (for Gemini → MCP OAuth)
This is the IdP your users sign into — Okta, Azure AD, Google Workspace, Ping, etc. Not Databricks.

| Value | Where to find / set | Example |
|---|---|---|
| Authorization URL | IdP OAuth app | `https://acme.okta.com/oauth2/default/v1/authorize` |
| Token URL | IdP OAuth app | `https://acme.okta.com/oauth2/default/v1/token` |
| Introspection URL | IdP OAuth app | `https://acme.okta.com/oauth2/default/v1/introspect` |
| OAuth client ID | IdP OAuth app | `0oa...` |
| OAuth client secret | IdP OAuth app | `secret-...` |
| Scopes | Must include `offline_access` | `openid offline_access groups` |
| Expected audience (optional) | If you enforce one | `databricks-mcp` |

---

## 1. GCP prerequisites **[once]**

```bash
PROJECT=acme-gemini-prod
REGION=us-central1

gcloud config set project $PROJECT

# Enable APIs
gcloud services enable \
    discoveryengine.googleapis.com \
    run.googleapis.com \
    cloudscheduler.googleapis.com \
    secretmanager.googleapis.com \
    firestore.googleapis.com \
    storage.googleapis.com \
    artifactregistry.googleapis.com

# Firestore (Native mode) — for the ingestion watermark
gcloud firestore databases create --location=$REGION --type=firestore-native

# GCS staging bucket for JSONL imports
gcloud storage buckets create gs://acme-gemini-staging --location=$REGION
```

### Org policy override for Custom MCP datastores **[once, org admin]**

Custom MCP Server datastores are blocked by an org policy constraint by default. An org admin must override it before step 6.

```bash
# Check current state
gcloud org-policies describe \
    constraints/discoveryengine.allowedCustomMcpDatastores \
    --project=$PROJECT

# Allow all (or scope to specific URLs)
cat > policy.yaml <<EOF
name: projects/$PROJECT/policies/discoveryengine.allowedCustomMcpDatastores
spec:
  rules:
  - allowAll: true
EOF
gcloud org-policies set-policy policy.yaml
```

### Admin IAM **[once]**

The person running the registration step needs:

- `roles/discoveryengine.editor` on the project
- `roles/run.admin` on the project
- `roles/iam.serviceAccountUser` to deploy services as the SAs below

---

## 2. Service accounts and IAM **[once]**

Three SAs, each with the minimum it needs.

```bash
# MCP server SA
gcloud iam service-accounts create mcp-server-sa \
    --display-name="Databricks MCP server"

# Ingestion + identity import SA (shared)
gcloud iam service-accounts create ingestion-sa \
    --display-name="Databricks ingestion"

# Scheduler SA (invokes the jobs)
gcloud iam service-accounts create scheduler-sa \
    --display-name="Cloud Scheduler"
```

| SA | Role | Resource |
|---|---|---|
| `mcp-server-sa` | `roles/secretmanager.secretAccessor` | Three secrets (see §3) |
| `ingestion-sa` | `roles/discoveryengine.editor` | Project |
| `ingestion-sa` | `roles/storage.objectAdmin` | `gs://acme-gemini-staging` |
| `ingestion-sa` | `roles/datastore.user` | Project (for Firestore watermark) |
| `ingestion-sa` | `roles/secretmanager.secretAccessor` | Databricks SP secrets |
| `scheduler-sa` | `roles/run.invoker` | Both Cloud Run Jobs |

Example bindings:

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')

for ROLE in roles/discoveryengine.editor roles/datastore.user; do
  gcloud projects add-iam-policy-binding $PROJECT \
      --member="serviceAccount:ingestion-sa@$PROJECT.iam.gserviceaccount.com" \
      --role=$ROLE
done

gcloud storage buckets add-iam-policy-binding gs://acme-gemini-staging \
    --member="serviceAccount:ingestion-sa@$PROJECT.iam.gserviceaccount.com" \
    --role=roles/storage.objectAdmin
```

---

## 3. Secret Manager **[once]**

```bash
# Databricks service principal OAuth client ID
printf '%s' '<sp-app-id>' | gcloud secrets create databricks-sp-client-id \
    --data-file=- --replication-policy=automatic

# Databricks service principal OAuth secret
printf '%s' '<sp-secret>' | gcloud secrets create databricks-sp-client-secret \
    --data-file=- --replication-policy=automatic

# Customer IdP OAuth client secret (for Gemini → MCP)
printf '%s' '<idp-client-secret>' | gcloud secrets create gemini-oauth-client-secret \
    --data-file=- --replication-policy=automatic
```

Grant Secret Accessor on each:

```bash
for SECRET in databricks-sp-client-id databricks-sp-client-secret gemini-oauth-client-secret; do
  gcloud secrets add-iam-policy-binding $SECRET \
      --member="serviceAccount:ingestion-sa@$PROJECT.iam.gserviceaccount.com" \
      --role=roles/secretmanager.secretAccessor
done

for SECRET in databricks-sp-client-id databricks-sp-client-secret gemini-oauth-client-secret; do
  gcloud secrets add-iam-policy-binding $SECRET \
      --member="serviceAccount:mcp-server-sa@$PROJECT.iam.gserviceaccount.com" \
      --role=roles/secretmanager.secretAccessor
done
```

---

## 4. Databricks setup **[once]**

In the Databricks account console:

1. **Service principal** — create one named something like `gemini-connector`. Generate an OAuth client secret (Account console → Service principals → *the SP* → Secrets → Generate secret). Note the client ID and secret; the secret is shown once.
2. **Workspace assignment** — assign the SP to the workspace.
3. **SCIM/account admin** — for the identity importer to list groups, the SP needs `account admin` *or* the workspace-level `Groups: Read` entitlement at the account level. Without this, `w.groups.list(...)` returns empty.

Permissions to grant the SP **inside** the workspace:

| Resource | Permission | Why |
|---|---|---|
| The SQL Warehouse | `CAN USE` | MCP server runs queries on it |
| Each schema you query | `USE SCHEMA` | Required by UC |
| Each table you query or index | `SELECT` | Reads for MCP and ingestion |
| Each UC volume you index | `READ VOLUME` | List + download files |
| `system.information_schema` (built-in) | already readable | `list_tables` / `describe_table` |

Warehouse hygiene the MCP path depends on:

- `AUTO_SUSPEND = 60` seconds — agent traffic is bursty; idle credits are wasted otherwise.
- Statement timeout — already enforced server-side via `DATABRICKS_STMT_TIMEOUT` (default 60s).

---

## 5. Customer IdP OAuth app **[once]**

Register Gemini Enterprise as an OAuth client in your IdP:

| Field | Value |
|---|---|
| Redirect URI | `https://vertexaisearch.cloud.google.com/oauth-redirect` (exact, no trailing slash) |
| Grant types | `authorization_code`, `refresh_token` |
| Scopes | Whatever your `auth.py` introspection expects; **must include `offline_access`** |
| Token endpoint auth | `client_secret_post` |

Without `offline_access` you'll get random 401s mid-conversation when access tokens expire.

---

## 6. Discovery Engine datastore + IMS **[once]**

`acl_enabled=True` can only be set at creation. If you skip it, you'll have to recreate the datastore and re-import everything.

```bash
cd ingestion-connector
pip install -e .

# Authenticate gcloud locally for the one-time setup
gcloud auth application-default login

python infra/create_datastore.py \
    --project $PROJECT \
    --location global \
    --ims-id databricks-ims \
    --datastore-id databricks-docs
```

If every reader is already a Google Workspace email (no Databricks-specific identities), add `--skip-ims` and don't create an IMS — but you can't add one later, so be sure.

---

## 7. Identity mappings **[once + ongoing]**

Author `infra/mappings.json` (use `infra/mappings.example.json` as a template):

```json
{
  "groups": [
    {"databricks_group": "data-engineers", "google_group": "data-eng@example.com"},
    {"databricks_group": "contractors", "expand_members": true}
  ],
  "user_overrides": {}
}
```

For each Databricks group your documents will ACL on, pick one:

- **`google_group`** — there's a Workspace group whose membership matches. Best.
- **`expand_members: true`** — no Workspace analog; fan out to current members.

Dry-run, then push:

```bash
export DATABRICKS_HOST=dbc-12345678-abcd.cloud.databricks.com
export DATABRICKS_CLIENT_ID=$(gcloud secrets versions access latest --secret=databricks-sp-client-id)
export DATABRICKS_CLIENT_SECRET=$(gcloud secrets versions access latest --secret=databricks-sp-client-secret)

python infra/import_identities.py \
    --project $PROJECT \
    --location global \
    --ims-id databricks-ims \
    --config infra/mappings.json \
    --dry-run

python infra/import_identities.py \
    --project $PROJECT \
    --location global \
    --ims-id databricks-ims \
    --config infra/mappings.json \
    --repurge-expanded
```

---

## 8. Deploy the MCP server **[per env]**

```bash
cd ../mcp-server

gcloud run deploy databricks-mcp \
    --source . \
    --region $REGION \
    --service-account mcp-server-sa@$PROJECT.iam.gserviceaccount.com \
    --allow-unauthenticated \
    --set-secrets "DATABRICKS_CLIENT_ID=databricks-sp-client-id:latest,\
DATABRICKS_CLIENT_SECRET=databricks-sp-client-secret:latest,\
OAUTH_CLIENT_SECRET=gemini-oauth-client-secret:latest" \
    --set-env-vars "DATABRICKS_HOST=dbc-12345678-abcd.cloud.databricks.com,\
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abcdef1234567890,\
OAUTH_INTROSPECT_URL=https://acme.okta.com/oauth2/default/v1/introspect,\
OAUTH_CLIENT_ID=0oa...,\
OAUTH_EXPECTED_AUDIENCE=databricks-mcp"
```

`--allow-unauthenticated` is correct — your OAuth check is the auth layer.

Capture the service URL — you'll paste it into Gemini Enterprise next.

### Register as a Custom MCP Server datastore

1. **Gemini Enterprise → Data stores → Create data store → Custom MCP Server**.
2. Fill in:
   - **MCP Server URL**: `https://<service-url>/mcp`
   - **Authorization URL**: from §0
   - **Token URL**: from §0
   - **Client ID / Client Secret**: from §0
   - **Scopes**: include `offline_access`
3. Click **Login** and complete the OAuth flow.
4. Pick a multi-region location, name the datastore (e.g. `databricks-mcp`).
5. After creation: **Datastore → Actions → Reload custom actions** → enable all four tools (`list_tables`, `describe_table`, `run_query`, `get_record`).

---

## 9. Deploy the ingestion connector **[per env]**

Build the image once (used by both the ingestion job and the identity import job):

```bash
cd ../ingestion-connector

# Artifact Registry repo (one-time)
gcloud artifacts repositories create connectors \
    --repository-format=docker --location=$REGION

# Build & push
gcloud builds submit \
    --tag $REGION-docker.pkg.dev/$PROJECT/connectors/databricks-ingestion:latest
```

Deploy the ingestion job **[per source]** — one job per `--watermark-key`:

```bash
gcloud run jobs deploy databricks-ingestion-policies \
    --image $REGION-docker.pkg.dev/$PROJECT/connectors/databricks-ingestion:latest \
    --region $REGION \
    --service-account ingestion-sa@$PROJECT.iam.gserviceaccount.com \
    --set-secrets "DATABRICKS_CLIENT_ID=databricks-sp-client-id:latest,\
DATABRICKS_CLIENT_SECRET=databricks-sp-client-secret:latest" \
    --set-env-vars "DATABRICKS_HOST=dbc-12345678-abcd.cloud.databricks.com,\
GCP_PROJECT=$PROJECT" \
    --args "--mode,incremental,\
--watermark-key,policies,\
--volume-root,/Volumes/main/docs/policies,\
--gcs-staging,gs://acme-gemini-staging/policies,\
--datastore-id,databricks-docs,\
--default-reader-group,policy_readers"
```

Run the initial `FULL` import once, manually:

```bash
gcloud run jobs execute databricks-ingestion-policies \
    --region $REGION \
    --args "--mode,initial,\
--watermark-key,policies,\
--volume-root,/Volumes/main/docs/policies,\
--gcs-staging,gs://acme-gemini-staging/policies,\
--datastore-id,databricks-docs,\
--default-reader-group,policy_readers"
```

Schedule incremental + weekly reconcile:

```bash
# Hourly incremental
gcloud scheduler jobs create http databricks-ingestion-policies-hourly \
    --schedule "0 * * * *" \
    --location $REGION \
    --uri "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/databricks-ingestion-policies:run" \
    --http-method POST \
    --oauth-service-account-email scheduler-sa@$PROJECT.iam.gserviceaccount.com

# Weekly reconcile (catches deletes + drift)
gcloud scheduler jobs create http databricks-ingestion-policies-weekly \
    --schedule "0 3 * * 0" \
    --location $REGION \
    --uri "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/databricks-ingestion-policies:run" \
    --http-method POST \
    --oauth-service-account-email scheduler-sa@$PROJECT.iam.gserviceaccount.com \
    --message-body '{"overrides":{"containerOverrides":[{"args":["--mode","reconcile","--watermark-key","policies","--volume-root","/Volumes/main/docs/policies","--gcs-staging","gs://acme-gemini-staging/policies","--datastore-id","databricks-docs","--default-reader-group","policy_readers"]}]}}'
```

Repeat per source (different `--watermark-key`, `--volume-root` or `--table-*` args).

---

## 10. Deploy the identity importer **[per env]**

Same image, different entrypoint, different schedule:

```bash
gcloud run jobs deploy databricks-identity-import \
    --image $REGION-docker.pkg.dev/$PROJECT/connectors/databricks-ingestion:latest \
    --region $REGION \
    --service-account ingestion-sa@$PROJECT.iam.gserviceaccount.com \
    --command "python" \
    --args "infra/import_identities.py,\
--project,$PROJECT,\
--location,global,\
--ims-id,databricks-ims,\
--config,infra/mappings.json,\
--repurge-expanded" \
    --set-secrets "DATABRICKS_CLIENT_ID=databricks-sp-client-id:latest,\
DATABRICKS_CLIENT_SECRET=databricks-sp-client-secret:latest" \
    --set-env-vars "DATABRICKS_HOST=dbc-12345678-abcd.cloud.databricks.com"

# 6-hour refresh
gcloud scheduler jobs create http databricks-identity-import-6h \
    --schedule "0 */6 * * *" \
    --location $REGION \
    --uri "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/databricks-identity-import:run" \
    --http-method POST \
    --oauth-service-account-email scheduler-sa@$PROJECT.iam.gserviceaccount.com
```

`mappings.json` must be in the image. Rebuild + redeploy the image whenever it changes — Cloud Build picks it up automatically since it's in the source tree.

---

## 11. Attach datastores to a Gemini Enterprise app **[per env]**

1. **Gemini Enterprise → Apps → Create app** (or edit one).
2. **Data stores → Add** → select both:
   - `databricks-mcp` (Custom MCP Server)
   - `databricks-docs` (Discovery Engine search datastore)
3. Save.

---

## 12. End-to-end validation

A short checklist to confirm everything is wired before handing off.

### MCP path
- [ ] `gcloud run services describe databricks-mcp` shows the service is healthy
- [ ] `curl https://<service-url>/mcp` returns 401 (auth working, no bearer token)
- [ ] In Gemini Enterprise: **Reload custom actions** shows 4 tools
- [ ] Ask the agent "What tables do you have access to in `main.sales`?" → invokes `list_tables`
- [ ] Ask "How many rows are in `main.sales.orders`?" → invokes `run_query`
- [ ] Try a `DELETE` — `run_query` should reject it before it hits Databricks

### Ingestion path
- [ ] `gcloud run jobs executions list --job databricks-ingestion-policies` shows the initial run succeeded
- [ ] Firestore: `ingestion_watermarks/policies` document exists with a recent timestamp
- [ ] GCS staging bucket has JSONL shards under the run prefix
- [ ] `gcloud beta discovery-engine documents list --data-store=databricks-docs` returns documents
- [ ] Ask the agent a question whose answer lives in a volume file → response includes a citation
- [ ] Test ACL: log in as a user *not* in the reader group → same question returns nothing or "I don't have access"

### Identity flow
- [ ] `gcloud run jobs executions list --job databricks-identity-import` shows success
- [ ] `gcloud beta discovery-engine identity-mappings list --identity-mapping-store=databricks-ims` shows entries for every group in `mappings.json`
- [ ] Removing a user from a Databricks group, then re-running with `--repurge-expanded`, removes their access within one cycle

---

## Common gotchas (one-line each)

- **`acl_enabled=True` was forgotten** → only fix is delete + recreate the datastore + re-import everything. Catch this in §6.
- **MCP `tools/list` returns 0 tools** → server is on SSE, not StreamableHTTP. The shipped `server.py` is correct; don't change the transport.
- **Random 401 mid-conversation** → IdP didn't issue a refresh token; missing `offline_access` scope.
- **Documents indexed but no results in queries** → ACL mismatch. Sanity-check with one `idp_wide:True` test doc.
- **Identity import returns 0 mappings for a group** → SP lacks account-level read for groups. Promote it to account admin or grant `Groups: Read`.
- **Duplicates after each ingestion run** → someone replaced the stable ID derivation with a UUID. IDs must be derived from the source primary key (`volume_path` or `catalog.schema.table:pk`).
- **`identity_mapping_store` not found at query time** → datastore was created without the IMS reference, or with the wrong format. Must be the full resource name.
- **Per-user Databricks row-level security isn't applying** → expected. M2M auth uses one identity; row-level security needs OAuth U2M (on-behalf-of).
