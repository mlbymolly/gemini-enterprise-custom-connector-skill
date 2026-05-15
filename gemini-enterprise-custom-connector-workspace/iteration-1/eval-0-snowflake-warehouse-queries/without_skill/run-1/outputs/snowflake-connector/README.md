# Snowflake -> Gemini Enterprise Connector

Scaffold for wiring Gemini Enterprise agents up to a Snowflake sales
warehouse using **Snowflake's managed MCP server** + **Gemini
Enterprise's Custom MCP Server data store**. This is the
Google-and-Snowflake-recommended path as of mid-2026.

## Why MCP and not a Discovery Engine document connector?

Sales data in a warehouse is *structured and live*. Replicating it
into a Discovery Engine document store (the other custom-connector
shape) gives you stale, denormalized snapshots and loses row-level
security. The MCP path keeps the data in Snowflake, runs governed
text-to-SQL through Cortex Analyst, and propagates the end-user
identity all the way to the warehouse via External OAuth.

## File map

| File | Purpose |
|------|---------|
| `sql/01_create_role_and_warehouse.sql` | Least-privilege role + XS warehouse |
| `sql/02_external_oauth_integration.sql` | OAuth trust to your IdP |
| `sql/03_semantic_view.sql` | Cortex Analyst semantic view over sales |
| `sql/04_create_mcp_server.sql` | The MCP server itself |
| `gemini_enterprise/data_store_config.yaml` | Values for the GE console |
| `scripts/register_data_store.sh` | Same registration, scripted |
| `scripts/smoke_test_mcp.py` | Verify the MCP endpoint locally |

## Run order

```
1. SQL 01 -> 02 -> 03 -> 04          (in Snowflake, as ACCOUNTADMIN)
2. Capture the mcp_server_url from   DESC MCP SERVER GE_SALES_MCP;
3. Register Gemini Enterprise as an OAuth client in your IdP
   (redirect: https://vertexaisearch.cloud.google.com/oauth-redirect)
4. scripts/register_data_store.sh    (or use the GE console UI)
5. In the GE console, ENABLE the 'sales_analyst' tool
6. scripts/smoke_test_mcp.py         (optional sanity check)
7. Attach the data store to your agent and ask:
   "What was net revenue by region last quarter?"
```

## Assumptions

- Your sales data lives in `SALES_DB.ANALYTICS` (rename throughout).
- You use Azure Entra ID or Google Cloud Identity as the IdP that
  fronts Gemini Enterprise. Other OIDC providers work the same way.
- Your project has had the org-policy constraint blocking custom
  MCP server data stores **overridden** (it's locked-down by default).
- You have `roles/discoveryengine.editor` on the GCP project.
