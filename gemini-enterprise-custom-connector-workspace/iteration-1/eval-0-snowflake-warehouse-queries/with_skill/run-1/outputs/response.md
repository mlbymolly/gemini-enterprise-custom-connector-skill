# Gemini Enterprise + Snowflake sales data — recommended approach

## Recommendation: custom MCP server (not an ingestion connector)

Gemini Enterprise has two extension points for sources it doesn't ship a native connector for:

1. **Custom MCP server** — Gemini calls your server as a tool **at query time**. Live data, full source semantics, supports writes.
2. **Custom connector → Discovery Engine datastore** — you push documents into a search index and Gemini grounds on the snapshot. Best for documents/RAG.

For a Snowflake warehouse holding sales data, the right answer is **MCP server**. The reasoning:

- Snowflake is a query engine over (potentially huge) structured data. Replicating it into a Discovery Engine search index doubles your storage cost, loses Snowflake's SQL semantics (joins, aggregations, window functions), and serves stale data.
- Sales questions ("what was last week's revenue by region?") require live state and SQL — exactly what an MCP `run_query` tool gives the agent.
- Ingestion is the wrong tool here. Reserve it for things like a stable product catalog or a glossary you want grounded with citations alongside other corpora — and if you have one of those, you can run both paths in parallel.

## Tool surface

I scaffolded four tools, in priority order from the Snowflake recipe:

| Tool | Why it's there |
| --- | --- |
| `list_tables(schema)` | Agent discovers what sales data exists before writing SQL. |
| `describe_table(table)` | Agent gets column names + types + sample values so its queries actually compile. |
| `run_query(sql, max_rows)` | The workhorse. SELECT-only, with SQL parsed by sqlglot (not regex), a server-injected `LIMIT`, and a 60s `STATEMENT_TIMEOUT_IN_SECONDS`. |
| `get_record(table, primary_key)` | Single-row lookups by PK (e.g. "look up order 12345"). |

No DDL or DML tools. If you later need writes, the recipe is firm: add **tightly-scoped** tools like `create_sales_note(order_id, body)`, never a generic `run_dml(sql)`.

## Auth model

Two auth boundaries to keep straight:

1. **Inbound (Gemini → MCP server):** OAuth 2.0 against your IdP (Okta, Azure AD, Google, etc.). Gemini forwards an `Authorization: Bearer ...` header on every call; `auth.py` validates it via the IdP introspection endpoint. The IdP must have `https://vertexaisearch.cloud.google.com/oauth-redirect` registered as a redirect URI exactly, and you must include `offline_access` in scopes so refresh tokens are issued.
2. **Outbound (MCP server → Snowflake):** Service account with key-pair auth (recipe **model 1**, the simplest). The MCP server holds the private key in Google Secret Manager and runs every query as `AGENT_SVC` under role `AGENT_READER`. ACL enforcement happens inside Snowflake via row-access policies. If you need per-user filtering (different agent users seeing different rows), switch to **model 2** (Snowflake External OAuth passthrough); the `source_client.py` is structured to make that swap easy.

## Hard requirements I baked in (these are non-negotiable for Gemini Enterprise MCP)

- **StreamableHTTP transport, not SSE.** Gemini's MCP client will refuse SSE. The server runs `mcp.run(transport="streamable-http", ..., path="/mcp")`.
- **HTTPS endpoint, conventionally `/mcp`.** Cloud Run gives you HTTPS automatically; that's why it's the deploy target.
- **No VPC-SC / no Private Service Connect** in preview — the MCP URL must be reachable from Google's public network. If your Snowflake account is behind a private link, the MCP server can still front it from Cloud Run with Serverless VPC Access.
- **Org policy override** for `constraints/discoveryengine.allowedCustomMcpDatastores` and the "Discovery Engine Editor" role on the admin. Your platform team has to do this once.

## Snowflake-side hygiene the skill calls out

- Warehouse `AGENT_WH` should be X-Small with `AUTO_SUSPEND = 60`. Agent traffic is bursty; idle credits are wasted.
- Role `AGENT_READER` gets USAGE on warehouse/database/schema and SELECT on the relevant tables/views — nothing else. The role itself is your blast-radius limit.
- `STATEMENT_TIMEOUT_IN_SECONDS = 60` is set at the session level so a runaway query can't burn a hole in your bill.
- `run_query` rejects anything that isn't a single `SELECT` or `WITH ... SELECT`, parsed with sqlglot in `snowflake` dialect. String matching ("does this contain INSERT?") is not enough — agents will surprise you.

## Project layout

Files are saved under `outputs/snowflake-mcp-connector/`:

```
snowflake-mcp-connector/
├── pyproject.toml          # fastmcp, snowflake-connector-python, sqlglot, secret manager
├── server.py               # FastMCP tools + StreamableHTTP runner on /mcp
├── auth.py                 # OAuth bearer-token validation via IdP introspection
├── source_client.py        # Snowflake key-pair auth + SQL guardrails
├── Dockerfile              # Cloud Run target
├── .env.example            # Required env vars
└── README.md               # Deploy + registration runbook
```

## Deployment flow (the runbook)

1. Create Snowflake service user `AGENT_SVC` with key-pair auth; load the private key PEM into Secret Manager.
2. Create role `AGENT_READER`; grant USAGE on warehouse/database/schema, SELECT on the sales tables; `GRANT ROLE AGENT_READER TO USER AGENT_SVC`.
3. Configure your IdP OAuth app: redirect URI exactly `https://vertexaisearch.cloud.google.com/oauth-redirect`, grants `authorization_code` + `refresh_token`, scopes including `offline_access`.
4. `gcloud run deploy snowflake-mcp-connector --source . --allow-unauthenticated ...` (the `--allow-unauthenticated` is correct — the OAuth check in `auth.py` is the auth boundary; Gemini's caller isn't a Google identity Cloud Run IAM can be granted to).
5. **Gemini Enterprise → Data stores → Create data store → Custom MCP Server.** Fill in MCP URL `https://<cloud-run-host>/mcp`, your IdP auth/token URLs, the client ID/secret, and scopes (with `offline_access`).
6. Click **Login** to complete the OAuth handshake, then create the datastore.
7. After creation: **Datastore → Actions → Reload custom actions.** This triggers `tools/list` against your server. Enable all four tools.
8. Attach the datastore to your Gemini Enterprise app.
9. Smoke-test with the MCP inspector locally before step 4: `npx @modelcontextprotocol/inspector python server.py`.

## Assumptions I made

1. Sales data lives in `ANALYTICS.SALES.*`. Override via `SNOWFLAKE_DATABASE` / schema arg if not.
2. **Auth model 1** (single Snowflake service account, ACLs via row-access policies inside Snowflake) is acceptable. Switch to OAuth passthrough only if individual agent users need different row visibility.
3. Deployment target is **Cloud Run**. GKE / a VM also work; the env-var contract is the same.
4. Your IdP supports RFC 7662 introspection. For Okta / Azure AD I'd swap `auth.py` to JWT signature verification in production for lower latency.
5. Read-only is sufficient. No write tools are exposed; if you need writes later, scope them tightly (`create_sales_note(...)`, not `run_dml(sql)`).

## Common failure modes to watch for

- `tools/list` returns 0 tools after "Reload custom actions" → server is on SSE, not StreamableHTTP. Verify `transport="streamable-http"`.
- OAuth redirect fails → redirect URI doesn't match `https://vertexaisearch.cloud.google.com/oauth-redirect` **exactly** (trailing slash, http vs https).
- Tools show up but Gemini "can't find" them mid-conversation → tool descriptions too vague. Gemini routes by description; the docstrings in `server.py` are written verb-first and resource-specific on purpose.
- Random 401s mid-session → missing `offline_access` scope, so no refresh token.

## File pointers

- `outputs/snowflake-mcp-connector/server.py` — MCP tool definitions and StreamableHTTP runner.
- `outputs/snowflake-mcp-connector/auth.py` — inbound OAuth validation.
- `outputs/snowflake-mcp-connector/source_client.py` — Snowflake client, SQL guardrails, key-pair auth.
- `outputs/snowflake-mcp-connector/Dockerfile` — Cloud Run image.
- `outputs/snowflake-mcp-connector/README.md` — deploy runbook + registration steps.
- `outputs/snowflake-mcp-connector/.env.example` — required env vars.
