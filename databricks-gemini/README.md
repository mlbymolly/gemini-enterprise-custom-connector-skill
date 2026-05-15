# Databricks ↔ Gemini Enterprise connectors

Two complementary Python connectors that together let a Gemini Enterprise agent both **query** and **search** a Databricks workspace.

- **`mcp-server/`** — Custom MCP Server that exposes Databricks SQL as live tools (`list_tables`, `describe_table`, `run_query`, `get_record`). Deployed to Cloud Run.
- **`ingestion-connector/`** — Fetch → Transform → Sync pipeline that pushes Unity Catalog volume files and reference table rows into a Discovery Engine datastore for grounded search. Deployed as a Cloud Run Job.

Both share the same Databricks service principal (OAuth M2M) and the same Identity Mapping Store for ACL resolution.

## Architecture

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

## Which connector handles what

| | `mcp-server/` | `ingestion-connector/` |
|---|---|---|
| Pattern | Live tool calling | Indexed search / RAG |
| Use it for | Ad-hoc SQL, lookups, dashboards-as-tools | Grounded search over UC volume files & reference tables |
| Freshness | Real-time | Snapshot, re-synced on a schedule |
| Citations | No (tool output) | Yes (Discovery Engine) |
| Write actions | Easy to add (currently read-only) | No |
| ACL model | SP hits Databricks as one identity; Gemini enforces user-level access before the call | Per-document ACLs on Discovery Engine, enforced at query time via the IMS |
| Scale ceiling | Bounded by warehouse concurrency | ~10–15 docs/sec — switch to BigQuery CDC pattern past ~100k docs |

## Repository layout

```
databricks-gemini/
├── README.md                          ← you are here
├── SETUP.md                           ← start-to-finish deploy guide
│
├── mcp-server/                        ← Path A: live SQL via MCP
│   ├── server.py                      FastMCP entrypoint, tool definitions
│   ├── auth.py                        OAuth bearer validation against customer IdP
│   ├── databricks_client.py           SQL Warehouse + WorkspaceClient (OAuth M2M)
│   ├── sql_safety.py                  sqlglot-based read-only enforcement
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── README.md
│
└── ingestion-connector/               ← Path B: indexed docs via Discovery Engine
    ├── connector.py                   Cloud Run Job entrypoint, runs one pass
    ├── databricks_fetcher.py          UC volume files + reference table rows
    ├── transform.py                   Source records → discoveryengine.Document
    ├── identity_mapping.py            Reusable IMS helpers
    ├── infra/
    │   ├── create_datastore.py        One-time: create IMS + datastore (acl_enabled=True)
    │   ├── import_identities.py       Pull Databricks groups, push mappings to IMS
    │   └── mappings.example.json      Config schema for identity import
    ├── Dockerfile
    ├── pyproject.toml
    └── README.md
```

## Quickstart

**Full deploy guide is in [`SETUP.md`](./SETUP.md).** The high-level order:

1. **Databricks** — create the service principal, generate an OAuth client secret, grant warehouse + UC permissions.
2. **GCP** — enable APIs, create the staging bucket and Firestore database, override the org policy that blocks Custom MCP datastores, create three service accounts (MCP, ingestion, scheduler).
3. **Secret Manager** — store the Databricks SP client ID/secret and the customer IdP client secret.
4. **Discovery Engine** — `python ingestion-connector/infra/create_datastore.py` creates the IMS and datastore with `acl_enabled=True`. This setting cannot be added later.
5. **Identity mappings** — author `mappings.json`, dry-run, then `python ingestion-connector/infra/import_identities.py`.
6. **MCP server** — `gcloud run deploy databricks-mcp --source mcp-server/ ...`, then register it as a Custom MCP Server datastore in the Gemini Enterprise console and complete the OAuth flow.
7. **Ingestion connector** — build the image, deploy a Cloud Run Job per source, run the initial `FULL` import manually, schedule hourly incrementals and a weekly reconcile.
8. **Identity importer** — deploy as its own Cloud Run Job (reuses the same image) with a 6-hour schedule.
9. **Attach both datastores** to your Gemini Enterprise app.

## Configuration

Both subprojects read configuration from environment variables. Secrets come from Secret Manager via `--set-secrets`; everything else is plain `--set-env-vars`.

### Shared (Databricks)

| Variable | Purpose |
|---|---|
| `DATABRICKS_HOST` | Workspace hostname (no scheme) |
| `DATABRICKS_CLIENT_ID` | Service principal OAuth client ID |
| `DATABRICKS_CLIENT_SECRET` | Service principal OAuth client secret |

### MCP server only

| Variable | Purpose |
|---|---|
| `DATABRICKS_HTTP_PATH` | SQL Warehouse HTTP path |
| `DATABRICKS_STMT_TIMEOUT` | Statement timeout in seconds (default 60) |
| `DATABRICKS_ABS_MAX_ROWS` | Hard row cap (default 1000) |
| `OAUTH_INTROSPECT_URL` | Customer IdP introspection endpoint |
| `OAUTH_CLIENT_ID` | OAuth client registered with the customer IdP |
| `OAUTH_CLIENT_SECRET` | Same |
| `OAUTH_EXPECTED_AUDIENCE` | Optional audience claim to enforce |

### Ingestion connector only

| Variable | Purpose |
|---|---|
| `GCP_PROJECT` | Defaults to `GOOGLE_CLOUD_PROJECT` |
| `DISCOVERY_ENGINE_LOCATION` | Defaults to `global` |

## Security model

### Authentication

Two independent OAuth flows. Don't confuse them:

- **Gemini → MCP server** uses the customer's own IdP (Okta, Azure AD, Google Workspace, Ping). Each Gemini user signs in there; their access token is forwarded as a bearer header on every MCP call. `auth.py` validates it against the IdP's introspection endpoint.
- **MCP server → Databricks** and **ingestion connector → Databricks** both use OAuth M2M with a single Databricks service principal. Credentials live in Secret Manager.

### Authorization

- **MCP path** — every query runs as the same Databricks SP. Row/column-level security inside Databricks treats every Gemini user identically. User-level access is enforced upstream by Gemini Enterprise app-level controls and by which tools you expose. If you need per-user enforcement inside Databricks, you need OAuth U2M (on-behalf-of token exchange) — non-trivial and not in this repo.
- **Ingestion path** — each document carries its own ACL on Discovery Engine. Databricks group names are translated to Google subjects at query time via the Identity Mapping Store. See `ingestion-connector/identity_mapping.py` and `infra/import_identities.py`.

### SQL safety

`run_query` rejects anything that isn't a single `SELECT` or `WITH ... SELECT` (parsed by `sqlglot`, not regex). It also injects a `LIMIT` if missing and caps rows server-side regardless of the caller's `max_rows`. `get_record` uses parameter binding, never string concatenation.

## Operating the connectors

| Task | Cadence | Command |
|---|---|---|
| Document incremental sync | Hourly | Cloud Scheduler → `databricks-ingestion-*` job (mode=incremental) |
| Document reconcile | Weekly | Cloud Scheduler → `databricks-ingestion-*` job (mode=reconcile) |
| Identity mapping refresh | 6 hours | Cloud Scheduler → `databricks-identity-import` job |
| Initial `FULL` import | One-time per source | `gcloud run jobs execute ... --args "--mode,initial,..."` |
| Add a new volume or table | As needed | New Cloud Run Job per `--watermark-key` |
| Rotate Databricks SP secret | Per your policy | `gcloud secrets versions add databricks-sp-client-secret --data-file=-` |

## When this isn't the right architecture

- **You have > ~100k documents OR need sub-hour freshness.** The single-Python-script ingestion pattern tops out around 10–15 docs/sec. Switch to the BigQuery CDC pattern (5-table architecture, MERGE-based delta detection, Patch API) — it benchmarks ~36k docs/sec.
- **Databricks is behind Private Link with no public egress.** The MCP server URL must be reachable from Google's public network. Custom MCP datastores don't support PSC or VPC Service Controls in preview. Workaround: put the MCP server in front of the firewall (Cloud Run + Serverless VPC Access).
- **You need to *write* to Databricks.** This repo is read-only. Adding a write tool is straightforward — scope it tightly (`create_alert`, not `run_dml`) — but the SP needs the corresponding Databricks privileges and you should think hard about whether one shared identity is what you want.
- **Row/column-level security in Databricks must apply per Gemini user.** Switch to OAuth U2M passthrough. Not implemented here.

## Further reading

- [`SETUP.md`](./SETUP.md) — full deploy walkthrough with copy-pasteable commands.
- [`mcp-server/README.md`](./mcp-server/README.md) — MCP-specific configuration, tool docs, and failure-mode reference.
- [`ingestion-connector/README.md`](./ingestion-connector/README.md) — ingestion modes, IMS flow, scheduling recipes, and the bare-vs-prefixed external identity gotcha.
