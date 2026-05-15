# Databricks MCP server

A Gemini Enterprise Custom MCP Server that exposes the Databricks SQL Warehouse as a set of tools.

## Tools

- `list_tables(catalog, schema)` — enumerate tables and views.
- `describe_table(catalog, schema, table)` — column schema.
- `run_query(sql, max_rows)` — read-only SELECT/CTE with row cap and `sqlglot` validation.
- `get_record(catalog, schema, table, key_column, key_value)` — parameterized single-row lookup.

DDL and DML are rejected by `sql_safety.assert_read_only`. The server caps rows at 1000 regardless of `max_rows`.

## Environment variables

Customer IdP (the OAuth provider Gemini Enterprise will redirect users through):

| Variable | Purpose |
|---|---|
| `OAUTH_INTROSPECT_URL` | IdP introspection endpoint |
| `OAUTH_CLIENT_ID` | OAuth client ID registered with the IdP |
| `OAUTH_CLIENT_SECRET` | OAuth client secret |
| `OAUTH_EXPECTED_AUDIENCE` | (optional) audience claim to enforce |

Databricks (service principal, OAuth M2M):

| Variable | Purpose |
|---|---|
| `DATABRICKS_HOST` | Workspace hostname, e.g. `dbc-xxxxxxxx-xxxx.cloud.databricks.com` |
| `DATABRICKS_HTTP_PATH` | SQL Warehouse HTTP path, e.g. `/sql/1.0/warehouses/abcdef1234567890` |
| `DATABRICKS_CLIENT_ID` | Service principal OAuth client ID |
| `DATABRICKS_CLIENT_SECRET` | Service principal OAuth client secret |
| `DATABRICKS_STMT_TIMEOUT` | (optional, default 60) statement timeout in seconds |
| `DATABRICKS_ABS_MAX_ROWS` | (optional, default 1000) hard row cap |

## Local test

```bash
pip install -e .
# set env vars from a .env or your shell
python server.py
# in another terminal:
npx @modelcontextprotocol/inspector --uri http://localhost:8080/mcp
```

Confirm `tools/list` returns four tools and each one runs end-to-end.

## Deploy to Cloud Run

```bash
gcloud run deploy databricks-mcp \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-secrets "DATABRICKS_CLIENT_ID=databricks-sp-client-id:latest,\
DATABRICKS_CLIENT_SECRET=databricks-sp-client-secret:latest,\
OAUTH_CLIENT_SECRET=gemini-oauth-client-secret:latest" \
  --set-env-vars "DATABRICKS_HOST=dbc-xxxx.cloud.databricks.com,\
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abcdef1234567890,\
OAUTH_INTROSPECT_URL=https://idp.example.com/oauth2/introspect,\
OAUTH_CLIENT_ID=gemini-mcp"
```

`--allow-unauthenticated` is correct — your OAuth check is the auth layer. Don't put Cloud Run IAM on top; Gemini Enterprise isn't a Google identity that can be granted invoker permissions.

## Register in Gemini Enterprise

Prereqs (need org-level permissions):

1. Override `constraints/discoveryengine.allowedCustomMcpDatastores` on the project.
2. Grant the admin `roles/discoveryengine.editor`.

Then:

1. **Gemini Enterprise → Data stores → Create data store → Custom MCP Server**.
2. **MCP Server URL**: `https://<cloud-run-url>/mcp`
3. Authorization URL, Token URL, Client ID, Client Secret: from your IdP's OAuth app.
4. Scopes: include `offline_access` so Gemini can refresh tokens. Add any IdP-specific scopes your introspection endpoint expects.
5. Register `https://vertexaisearch.cloud.google.com/oauth-redirect` as a redirect URI in the IdP — exact match, no trailing slash.
6. Complete the OAuth flow with the **Login** button.
7. After creation: **Datastore → Actions → Reload custom actions**. This calls `tools/list`. Enable all four tools.
8. Attach the datastore to your Gemini Enterprise app.

## Failure-mode quick reference

- `tools/list` returns 0 tools → server isn't on StreamableHTTP. The `mcp.run(transport="streamable-http", ...)` line in `server.py` is the only correct setting; do not switch to SSE.
- OAuth redirect fails → redirect URI mismatch in the IdP. Must be exactly `https://vertexaisearch.cloud.google.com/oauth-redirect`.
- Gemini says "can't find a tool that does X" → docstring is too vague. Each tool's first sentence must say what it does in concrete terms.
- 401 mid-conversation → missing `offline_access` scope; access tokens expire and Gemini can't refresh.
- `run_query` rejects something that looks safe → the `sqlglot` parser doesn't recognize a Databricks-specific construct. Either rewrite the query or extend `sql_safety.py`.
