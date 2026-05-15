# Snowflake MCP connector for Gemini Enterprise

Live, OAuth-protected MCP server that lets Gemini Enterprise agents query a
Snowflake sales warehouse via four tools: `list_tables`, `describe_table`,
`run_query`, `get_record`.

## Why MCP (not an ingestion connector)

Snowflake is a query engine over very large structured data. Replicating the
warehouse into a Discovery Engine search index would double the storage cost,
lose Snowflake's SQL semantics, and serve stale data. An MCP server exposes
Snowflake live, which is what agents asking sales questions actually need.

Exception: if you also have a small reference table (e.g. product catalog
< 50k rows) that you want grounded with citations, ingest that one table
separately into a Discovery Engine datastore. Don't ingest the warehouse.

## Architecture

```
Gemini Enterprise ──OAuth──▶ Okta / Azure AD / etc.
        │
        │ StreamableHTTP (Bearer token)
        ▼
   This MCP server (Cloud Run, HTTPS, /mcp)
        │
        │ key-pair auth, AGENT_READER role, AGENT_WH warehouse
        ▼
   Snowflake (ANALYTICS database, SALES schema)
```

## Prerequisites

GCP / Gemini Enterprise:

- Discovery Engine API enabled on the project.
- The org policy constraint blocking custom MCP datastores is overridden.
- Admin has the "Discovery Engine Editor" role.
- A Secret Manager secret holding the Snowflake private key PEM.

Snowflake:

- Service user `AGENT_SVC` with key-pair auth configured.
- Role `AGENT_READER` granted USAGE on the warehouse, USAGE on the database,
  USAGE on the schema, and SELECT on the relevant tables/views — nothing else.
- Warehouse `AGENT_WH` sized X-Small with `AUTO_SUSPEND = 60`.
- Row-access policies on any tables that need per-user filtering (agent traffic
  all comes in as `AGENT_SVC` in this auth model).

IdP (Okta / Azure AD / GCP Identity Platform / etc.):

- OAuth app registered with redirect URI **exactly**
  `https://vertexaisearch.cloud.google.com/oauth-redirect` (no trailing slash).
- Grant types: `authorization_code`, `refresh_token`.
- Scopes defined for your tools; include `offline_access`.
- Token endpoint introspection enabled (or switch `auth.py` to JWT verification).

## Local development

```bash
pip install -e .
export OAUTH_INTROSPECT_URL=https://your-idp/oauth2/v1/introspect
export OAUTH_CLIENT_ID=...
export OAUTH_CLIENT_SECRET=...
export SNOWFLAKE_ACCOUNT=acme-corp
export SNOWFLAKE_USER=AGENT_SVC
export SNOWFLAKE_WAREHOUSE=AGENT_WH
export SNOWFLAKE_DATABASE=ANALYTICS
export SNOWFLAKE_ROLE=AGENT_READER
export SNOWFLAKE_PRIVATE_KEY_SECRET=projects/123/secrets/snowflake-agent-key/versions/latest

python server.py
# Then in another shell:
npx @modelcontextprotocol/inspector http://localhost:8080/mcp
```

Use the inspector to confirm `tools/list` returns four tools and the
descriptions read cleanly — Gemini routes on tool description, so that text is
load-bearing.

## Deploy to Cloud Run

```bash
gcloud run deploy snowflake-mcp-connector \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --service-account snowflake-mcp@PROJECT.iam.gserviceaccount.com \
  --set-env-vars "OAUTH_INTROSPECT_URL=...,OAUTH_CLIENT_ID=...,SNOWFLAKE_ACCOUNT=...,SNOWFLAKE_USER=AGENT_SVC,SNOWFLAKE_WAREHOUSE=AGENT_WH,SNOWFLAKE_DATABASE=ANALYTICS,SNOWFLAKE_ROLE=AGENT_READER,SNOWFLAKE_PRIVATE_KEY_SECRET=projects/123/secrets/snowflake-agent-key/versions/latest" \
  --set-secrets "OAUTH_CLIENT_SECRET=oauth-client-secret:latest"
```

`--allow-unauthenticated` is correct here — the OAuth check inside `auth.py`
is the auth boundary. Don't layer Cloud Run IAM on top; Gemini Enterprise's
caller is not a Google identity it can be granted.

The service account on the Cloud Run service needs `roles/secretmanager.secretAccessor`
on the Snowflake key secret.

## Register the datastore in Gemini Enterprise

1. **Gemini Enterprise → Data stores → Create data store → Custom MCP Server**.
2. Fill in:
   - **MCP Server URL**: `https://<cloud-run-host>/mcp`
   - **Authorization URL**: IdP base auth URL (no query params)
   - **Token URL**: IdP token URL
   - **Client ID / Secret**: from the OAuth app
   - **Scopes**: space-separated, include `offline_access`
3. Click **Login** to complete the OAuth flow.
4. Pick a multi-region location; name the datastore.
5. After creation: **Datastore → Actions → Reload custom actions**. This calls
   `tools/list`. Enable all four tools.
6. Attach the datastore to your Gemini Enterprise app.

## What's intentionally NOT here

- DDL / DML tools. The Snowflake recipe is explicit: don't expose `run_dml(sql)`.
  If you need writes, add tightly-scoped tools (`create_sales_note(order_id, body)`)
  not a generic mutation endpoint.
- Per-user Snowflake auth (OAuth passthrough). All queries run as `AGENT_SVC`.
  ACL enforcement must happen inside Snowflake via row-access policies on the
  service user. To switch to per-user enforcement, follow recipe model 2:
  configure Snowflake External OAuth and pass `user_claims` through to
  `_connect()` as `authenticator="oauth"`, `token=<forwarded_token>`.
- VPC Service Controls / Private Service Connect. Neither is supported by
  Gemini Enterprise MCP datastores in preview; the Cloud Run service must be
  reachable from Google's public network.

## Assumptions made while scaffolding

1. Sales data lives in `ANALYTICS.SALES.*` (overridable via env vars).
2. Service-account / key-pair auth is sufficient (auth model 1). You only need
   OAuth passthrough if different agent users must see different rows.
3. The deployment target is Cloud Run. GKE or a VM also work; the env-var
   contract is the same.
4. The IdP supports RFC 7662 token introspection. For Okta / Azure AD you can
   swap `auth.py` to JWT signature verification for lower latency.
