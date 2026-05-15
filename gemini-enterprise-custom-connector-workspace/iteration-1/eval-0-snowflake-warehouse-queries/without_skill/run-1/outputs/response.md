# Letting Gemini Enterprise agents query your Snowflake sales warehouse

## TL;DR

Don't build a Discovery-Engine "document" connector for warehouse data
- it's the wrong shape. The right approach in 2026 is:

> **Stand up a Snowflake-managed MCP server in front of a Cortex
> Analyst semantic view, then register it in Gemini Enterprise as a
> *Custom MCP Server* data store with user-delegated OAuth.**

That gives your agents governed, live, text-to-SQL access to sales
data without copying a single row out of Snowflake, and every query
runs as the *end user* (so row-level security and masking just work).

A complete scaffold lives next to this file in
`snowflake-connector/`. The rest of this doc explains why this is the
right shape, walks you through the moving parts, and points out the
traps.

---

## Assumptions I made

- "Gemini Enterprise" means the Google Cloud product (Discovery
  Engine + Agent Builder), not the consumer Gemini app.
- Your sales data is structured (star schema or close to it) and
  lives in Snowflake - the canonical "FACT_ORDERS + dims" pattern.
  Rename `SALES_DB.ANALYTICS` to whatever you actually use.
- Your IdP is Azure Entra ID or Google Cloud Identity. Anything OIDC
  works the same way; only URLs change.
- You can override the GCP org policy that blocks custom MCP server
  data stores (it's off by default in regulated tenants). If you
  can't, see the fallbacks at the end.

---

## Why this shape (and why not the alternatives)

Gemini Enterprise's "custom connector" surface area actually has
**three** distinct shapes, and people pick the wrong one constantly:

| Shape | What it is | Right for sales data? |
|---|---|---|
| **Custom data-source connector** (Fetch/Transform/Sync pipeline writing Discovery Engine documents) | A scheduled ETL job that flattens rows into JSON documents and uploads them | **No.** Stale snapshots, loses row-level security, can't answer aggregate questions ("revenue by region last quarter") because each row is an island. |
| **Custom MCP server data store** | Gemini Enterprise speaks the Model Context Protocol over Streamable-HTTP to a server *you* control; the server exposes tools the agent's planner calls live | **Yes - this is the one.** Live queries, identity propagation, no replication, governed by Snowflake's existing RBAC. |
| **Agent action / OpenAPI tool** | OpenAPI spec wrapping the Snowflake SQL API behind a Cloud Run proxy | Works for narrow, well-known queries; *worse* than MCP for ad-hoc analytics because the agent has to author SQL blind. Reasonable only if MCP is blocked. |

Snowflake now ships a **managed MCP server** as a first-class object
(`CREATE MCP SERVER ...`). It runs inside your Snowflake account, has
no infrastructure for you to operate, and exposes Cortex Analyst
(text-to-SQL over a semantic view) and Cortex Search as tools. Gemini
Enterprise's "Custom MCP Server (Preview)" data store is exactly the
client side of this.

---

## The end-to-end architecture

```
+-----------------+   user prompt        +----------------------+
|  End user in    |  ----------------->  |  Gemini Enterprise   |
|  Gemini Ent.    |                      |  (agent + planner)   |
+-----------------+                      +----------+-----------+
                                                    | MCP over
                                                    | Streamable-HTTP
                                                    | (JWT in header)
                                                    v
+----------------------------+   tools/list   +-------------------+
|  IdP (Entra ID / Google)   | <----OAuth---- |  Snowflake MCP    |
|  - Gemini = OAuth client   |                |  Server           |
|  - User authenticates once |                |  (in your account)|
+----------------------------+                +---------+---------+
                                                        |
                                                        v
                                              +-------------------+
                                              |  Cortex Analyst   |
                                              |  + Semantic View  |
                                              +---------+---------+
                                                        |
                                                        v
                                              +-------------------+
                                              |  SALES_DB.ANALYTICS|
                                              |  FACT_ORDERS, etc. |
                                              +-------------------+
```

Two things are doing a lot of work in that picture:

1. **External OAuth integration in Snowflake.** The JWT minted by your
   IdP for Gemini Enterprise is passed by the MCP server straight into
   the Snowflake session, mapped to a real Snowflake user via the
   `upn` (or `email`) claim. Result: queries run *as the user*, so
   row-access policies, masking policies, and column-level security
   apply automatically. No service-account-with-superuser-grant
   anti-pattern.
2. **Cortex Analyst semantic view.** The MCP tool's `type` is
   `CORTEX_ANALYST_MESSAGE`, which means the agent doesn't have to
   write SQL - it sends a natural-language message, Cortex Analyst
   generates governed SQL against the *semantic* layer (business
   names, synonyms, blessed metrics), runs it, and returns rows +
   the SQL it ran (so you can audit). This is dramatically more
   reliable than letting the LLM write SQL against raw tables.

---

## The scaffold

Everything below is in `snowflake-connector/` next to this file.

```
snowflake-connector/
  README.md
  sql/
    01_create_role_and_warehouse.sql      Least-priv role + XS WH
    02_external_oauth_integration.sql     IdP trust
    03_semantic_view.sql                  Cortex Analyst layer
    04_create_mcp_server.sql              The MCP server itself
  gemini_enterprise/
    data_store_config.yaml                Values for the GE console
  scripts/
    register_data_store.sh                Same registration via API
    smoke_test_mcp.py                     Local MCP client probe
```

### The key bits

**`04_create_mcp_server.sql`** is the heart of it:

```sql
CREATE OR REPLACE MCP SERVER GE_SALES_MCP
FROM SPECIFICATION
$$
tools:
  - name: "sales_analyst"
    type: "CORTEX_ANALYST_MESSAGE"
    identifier: "SALES_DB.ANALYTICS.SALES_SEMANTIC_VIEW"
    title: "Sales data Q&A"
    description: |
      Use this tool to answer ANY natural-language question about sales,
      revenue, orders, customers, products, segments, regions, or
      fiscal periods. ...
$$;
GRANT USAGE ON MCP SERVER GE_SALES_MCP TO ROLE GE_AGENT_ROLE;
DESC MCP SERVER GE_SALES_MCP;   -- copy mcp_server_url from output
```

The `description:` on the tool isn't decoration - Gemini Enterprise's
planner reads it when deciding which tool to call, so it's effectively
the system prompt for *when to route to Snowflake*. Be specific about
what's in scope and what isn't.

**`gemini_enterprise/data_store_config.yaml`** captures every value
you'll paste into the GE console (or feed to the registration script):

- MCP URL (from `DESC MCP SERVER`)
- Transport: **must be `STREAMABLE_HTTP`** - the old SSE transport is
  not supported by this connector.
- OAuth authorization URL, token URL, client ID/secret, scopes
- A long-form `description` for the *server itself* (separate from
  the per-tool descriptions)

---

## Step-by-step deployment

1. **GCP prep.** Override the org policy
   `constraints/gemini.disableCustomMcpServerDataStores` if it's
   enforced. Grant yourself `roles/discoveryengine.editor`.
2. **IdP prep.** Register Gemini Enterprise as an OAuth client.
   Redirect URI must be exactly
   `https://vertexaisearch.cloud.google.com/oauth-redirect`. Expose a
   scope like `session:role-any` and include `offline_access`.
3. **Snowflake prep.** Run the four SQL files in order. The External
   OAuth integration is what allows the JWT from step 2 to be
   exchanged for a Snowflake session.
4. **Define the semantic view.** Edit `03_semantic_view.sql` to match
   your real tables, columns, metrics and synonyms. *This file is
   where you get the most leverage* - the better the semantic layer,
   the better the agent's answers.
5. **Create the MCP server** (`04_create_mcp_server.sql`) and copy
   the `mcp_server_url` out of `DESC MCP SERVER`.
6. **Register the data store in Gemini Enterprise** - either via the
   console (`Data stores -> Create -> Custom MCP Server (Preview)`)
   or via `scripts/register_data_store.sh`.
7. **Enable the `sales_analyst` tool.** By default *no* tools from a
   custom MCP server are enabled - this is a deliberate safety
   default and it's the #1 reason "my connector is registered but the
   agent ignores it." Go to the Actions tab and flip it on.
8. **Smoke-test** with `scripts/smoke_test_mcp.py` (mints a token via
   client_credentials for a test principal, lists tools, calls
   `sales_analyst` once).
9. **Attach to your agent** in the GE console, and try:
   *"What was net revenue by region last quarter, with year-over-year
   change?"*

---

## Production checklist

- [ ] Warehouse is XS, auto-suspend 60s, **statement timeout set**
  (`ALTER WAREHOUSE ... SET STATEMENT_TIMEOUT_IN_SECONDS = 60`) so a
  runaway agent query can't burn credits.
- [ ] Resource monitor on `GE_AGENT_WH` with hard credit cap.
- [ ] `GE_AGENT_ROLE` has **no** writes anywhere. The optional
  `run_sql` tool is wired to a read-only context.
- [ ] Row-access policies on `FACT_ORDERS` keyed off
  `CURRENT_USER()` / `IS_ROLE_IN_SESSION()` - because the user
  identity propagates, these now actually do something.
- [ ] Masking policies on PII columns (`customer_email`,
  `phone_number`, etc).
- [ ] Query tag set so usage shows up cleanly in
  `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`:
  `ALTER USER ... SET QUERY_TAG = 'gemini-enterprise';`
- [ ] Cortex Analyst feedback turned on so business users can flag
  bad SQL generations - then iterate on the semantic view.
- [ ] OAuth client secret rotated and stored in Secret Manager, not
  the YAML.
- [ ] Audit logging: Snowflake's `QUERY_HISTORY` plus Discovery
  Engine audit logs in Cloud Logging.

---

## When to *not* use this

- **You're on a Snowflake edition without Cortex** (Standard without
  Cortex enabled, or a region where Cortex isn't GA). Fall back to
  an OpenAPI agent action that wraps the Snowflake SQL API
  `submitStatement` endpoint behind a Cloud Run proxy. You lose the
  text-to-SQL quality but keep live access.
- **You can't override the custom-MCP-server org policy.** Same
  fallback - OpenAPI agent action - or use a partner connector
  (CData Connect AI exposes Snowflake as a remote MCP server hosted
  by CData, which sidesteps the org-policy block on *custom* MCP
  servers).
- **You actually wanted unstructured search** (sales call
  transcripts, contract PDFs) rather than warehouse rows. Then a
  Discovery Engine document connector backed by Cortex Search is the
  right call - different scaffold entirely.

---

## Sources

- [Set up your custom MCP server data store - Gemini Enterprise docs](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server)
- [Write effective MCP server descriptions and instructions - Gemini Enterprise docs](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/writing-mcp-server-descriptions)
- [Create custom connector - Gemini Enterprise docs](https://docs.cloud.google.com/gemini/enterprise/docs/create-custom-connector)
- [Snowflake-managed MCP server docs](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-mcp)
- [CREATE MCP SERVER SQL reference](https://docs.snowflake.com/en/en/sql-reference/sql/create-mcp-server)
- [Snowflake MCP Connectors - Cortex Agents](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-mcp-connectors)
- [Cortex Analyst semantic model spec](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/semantic-model-spec)
- [Using Gemini Enterprise & connecting to Snowflake's MCP Server (Rajat Gupta, Medium)](https://medium.com/@rajatpgupta/connecting-snowflakes-mcp-server-5513a44b6f11)
- [Building a Production-Grade Snowflake AI Agent using MCP Server with Cortex AI](https://medium.com/towards-data-engineering/building-a-production-grade-snowflake-ai-agent-using-mcp-server-with-cortex-ai-d5253ee1305d)
- [CData: Connect Snowflake Data to Gemini Enterprise via Connect AI](https://www.cdata.com/kb/tech/snowflake-cloud-gemini-enterprise.rst)
- [Snowflake Cortex AI + Gemini 3 announcement](https://www.snowflake.com/en/news/press-releases/snowflake-enables-enterprise-ready-ai-by-bringing-google-s-gemini-3-to-snowflake-cortex-ai/)
