# Databricks ↔ Gemini Enterprise connectors

Two complementary connectors that together let a Gemini Enterprise agent both **query** and **search** a Databricks workspace.

```
                       ┌─────────────────────────────────┐
                       │      Gemini Enterprise app      │
                       └────────────────┬────────────────┘
                                        │
                ┌───────────────────────┴───────────────────────┐
                ▼                                               ▼
   ┌─────────────────────────┐                  ┌──────────────────────────────┐
   │  MCP server datastore   │                  │  Discovery Engine datastore  │
   │  (Cloud Run, /mcp)      │                  │  (acl_enabled=True)          │
   └────────────┬────────────┘                  └──────────────┬───────────────┘
                │ Databricks SQL                                ▲ import_documents
                │ (OAuth M2M, SP)                               │ (GCS JSONL)
                ▼                                               │
   ┌─────────────────────────┐                  ┌──────────────┴───────────────┐
   │  Databricks SQL         │                  │  Cloud Run Job (scheduled)   │
   │  Warehouse              │                  │  Fetch → Transform → Sync    │
   └─────────────────────────┘                  └──────────────┬───────────────┘
                                                               │ Databricks SDK
                                                               ▼
                                                  ┌─────────────────────────────┐
                                                  │  Unity Catalog volumes /    │
                                                  │  reference tables           │
                                                  └─────────────────────────────┘
```

| | `mcp-server/` | `ingestion-connector/` |
|---|---|---|
| Pattern | Live tool calling | Indexed search / RAG |
| Use it for | Ad-hoc SQL, lookups, dashboards-as-tools | Grounded search over UC volume files & reference tables |
| Freshness | Real-time | Snapshot, re-synced on a schedule |
| Citations | No (tool output) | Yes (Discovery Engine) |
| ACL model | Service principal hits Databricks; Gemini enforces user-level access before the call | Per-document ACLs on Discovery Engine, enforced at query time |

Both subprojects use the same Databricks service principal (OAuth M2M). Put the client ID / secret in Google Secret Manager once and reference it from both.

## Deploy order

1. Stand up the service principal in Databricks and grant it: SQL Warehouse `CAN USE`, the relevant schemas/tables `SELECT`, and `READ VOLUME` on any Unity Catalog volume you want to index.
2. Store `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET` in Secret Manager.
3. Deploy `mcp-server/` to Cloud Run (see its README). Register it as a Custom MCP Server datastore in Gemini Enterprise.
4. Deploy `ingestion-connector/` as a Cloud Run Job (see its README). Trigger the initial `FULL` import. Schedule incremental runs.
5. Attach both datastores to your Gemini Enterprise app.

## ACL note on M2M auth

With OAuth M2M, **one** Databricks identity (the service principal) executes every query. That means:

- Databricks row/column-level security treats every Gemini user the same.
- User-level access must be enforced *before* the call — by Gemini Enterprise's app-level access controls and by which tools you expose.
- If you need per-user enforcement inside Databricks, you'll need OAuth U2M (on-behalf-of) — a non-trivial token-exchange setup.
