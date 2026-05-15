# Snowflake recipe

## Pick the path

For Snowflake, the right path is almost always **MCP server**, not ingestion connector.

- Snowflake is a query engine over (potentially huge) structured data. Replicating it into a Discovery Engine index is the wrong tool — you'd be paying to materialize the warehouse twice and lose Snowflake's query semantics in the process.
- An MCP server with a `run_query` tool gives the agent live access with full SQL power.

The exception: if there's a small, slow-changing **reference table** (e.g., a glossary, a product catalog of <50k rows) that you want grounded with citations alongside other corpora, ingest just that table. Don't ingest the whole warehouse.

## MCP tool surface

Common tools, in priority order:

- `list_tables(schema: str)` — what can the agent see?
- `describe_table(table: str)` — column names, types, sample values.
- `run_query(sql: str, max_rows: int)` — read-only SELECT, with row cap.
- `get_record(table: str, primary_key: str)` — single-row lookup if there's a natural PK.

**Don't** expose DDL or DML tools by default. If the user wants write actions, scope them tightly — `create_ticket(title, body)` not `run_dml(sql)`.

## Snowflake auth — picking a model

Three viable models:

1. **Service account, single warehouse role** (simplest). MCP server holds a Snowflake user/password (or key pair) in Secret Manager. Every query runs as the same Snowflake user. All ACL enforcement happens in Snowflake row-access policies. Good when ACLs are uniform per-tenant.
2. **OAuth passthrough**. Use Snowflake's External OAuth. The MCP server forwards the user's OAuth token (from the IdP) to Snowflake. Snowflake resolves the Snowflake user from the token claims and enforces user-level policies. Best when ACLs vary per user.
3. **Personal access tokens (PAT)**. Each agent user gets their own Snowflake PAT, stored against their identity in Secret Manager. The MCP server looks up the PAT per request. Works but operationally heavy.

Default to model 1 unless the user explicitly needs per-user enforcement.

## Snowflake-specific guardrails to bake into `run_query`

- Reject anything that isn't a `SELECT` or `WITH ... SELECT`. Parse the SQL — don't rely on string matching.
- Always inject a `LIMIT max_rows`. Snowflake will happily stream a billion rows back to you otherwise.
- Set a statement timeout (`STATEMENT_TIMEOUT_IN_SECONDS = 60` at session level) so a runaway query doesn't burn credits.
- Choose a warehouse with `AUTO_SUSPEND = 60` — agent traffic is bursty and idle credits are wasted.

## Python client

```python
import snowflake.connector

def get_conn(user_token=None):
    return snowflake.connector.connect(
        account="acme-corp",
        warehouse="AGENT_WH",
        database="ANALYTICS",
        # Option 1 — service account auth:
        user="agent_svc",
        private_key=load_pem_from_secret_manager(),
        # Option 2 — OAuth passthrough:
        # authenticator="oauth",
        # token=user_token,
    )
```

## When to ingest instead

If you do decide to ingest a small reference table:

- One `Document` per row.
- `id = f"snowflake:{db}.{schema}.{table}:{pk}"`.
- `content.raw_bytes` = a rendered description of the row (e.g., "Product: X. Category: Y. Description: Z."). Don't just dump the JSON — Gemini grounds on text.
- Sync nightly via Snowflake Tasks → Cloud Storage export → GCS import.
