---
name: gemini-enterprise-custom-connector
description: How to build a custom connector for Gemini Enterprise to make a data source queryable that isn't in the native connector library (e.g., Snowflake, SAP, internal databases, proprietary REST APIs). Covers the two supported paths — Custom MCP Server (live tool calling) and Custom Connector → Discovery Engine datastore (indexed search/RAG) — when to use each, scaffolding, Python SDK calls, OAuth/ACL configuration, and registering with a Gemini Enterprise app. Use this skill whenever the user mentions Gemini Enterprise alongside any source that isn't in the natively supported list (Salesforce, ServiceNow, SharePoint, Jira, Confluence, Drive, Slack, GitHub, Notion, Box, Dropbox, etc.) — even without the words "custom connector." Also use it for "scaffold a connector," "make X searchable in Gemini Enterprise," "ingest X into a Gemini datastore," "expose X as a tool to a Gemini agent," or any question about Gemini Enterprise reading from Snowflake / SAP / a warehouse / an internal API.
---

# Custom connectors for Gemini Enterprise

Gemini Enterprise ships a library of native connectors (Salesforce, ServiceNow, SharePoint, Jira, Confluence, Drive, Gmail, Slack, GitHub, Notion, Box, Dropbox, Zendesk, HubSpot, Shopify, and ~40 more), plus first-party Google data sources (BigQuery, Cloud SQL, Spanner, etc.). When a source isn't in that library, two extension points fill the gap:

1. **Custom MCP Server** — Gemini calls your server as a tool *at query time*. Best for live lookups and write actions.
2. **Custom Connector → Discovery Engine datastore** — You push documents into an indexed datastore. Best for grounded search, summarization, and RAG over a snapshot of the source.

Pick the wrong one and the user pays for it later (wrong latency profile, wrong cost model, wrong freshness, wrong ACL semantics). Help the user decide before you write any code.

## Which path does the user need?

Ask these three questions, in this order:

1. **Does the agent need to read the source's *current* state at query time, or is a periodically-refreshed snapshot acceptable?**
   - "Current state" → MCP server.
   - "Snapshot" → Ingestion connector.

2. **Does the agent need to *write* back to the source (create a ticket, run a query, post a message)?**
   - Yes → MCP server (ingestion connectors are read-only into the datastore).
   - No → Either path works; let the next question decide.

3. **Does the user want the answers grounded with citations and the content searchable across the agent's other indexed corpora?**
   - Yes → Ingestion connector. Discovery Engine indexes the content alongside everything else and produces citations.
   - No → MCP server is simpler to operate.

If the answers conflict, you can do both — an MCP server for write actions and live queries, plus an ingestion connector for the bulk read corpus. Many real deployments end up with both.

A worked example: for **Snowflake** holding a billion-row events table, an ingestion connector is wrong (you'd be replicating a warehouse into a search index — wrong tool). The right pattern is an MCP server that exposes a `run_snowflake_query` tool. For **SAP** holding policy documents and HR forms, an ingestion connector is right — the documents are stable, you want grounded retrieval, and ACLs map naturally to user/group readers.

## Path A: Custom MCP Server

A Gemini Enterprise MCP datastore is an OAuth-protected HTTPS endpoint that speaks the Model Context Protocol over StreamableHTTP. Gemini calls `tools/list` to discover what's available and calls each tool as needed during a session.

**Hard requirements** (these will bite you if missed — verify before writing code):

- **Transport**: StreamableHTTP only. SSE is *not* supported. If you're starting from an MCP example that uses SSE, you must switch.
- **Endpoint**: HTTPS, conventionally ending in `/mcp` (e.g. `https://mcp.example.com/mcp`). Plain HTTP and self-signed certs won't work.
- **Auth**: OAuth 2.0. You need an Authorization URL, Token URL, Client ID/Secret, and scopes (use `offline_access` if you want refresh tokens).
- **OAuth redirect**: Register `https://vertexaisearch.cloud.google.com/oauth-redirect` as a callback in your IdP.
- **Networking**: Private Service Connect and VPC Service Controls are *not* supported in preview. If the source is locked inside a VPC with no public ingress, MCP is the wrong path today — use ingestion instead.
- **GCP prereqs**: The org policy constraint that blocks custom MCP datastores must be overridden, and the admin needs the "Discovery Engine Editor" role.

**Scaffold steps** — when the user asks you to build one, do roughly this in order:

1. Confirm the tool surface with the user (what should the agent be able to *do*?). Each tool becomes one MCP `tool` with a JSON Schema input. Keep tool names verb-led and inputs minimal — Gemini routes to tools by name+description, so a vague description is a routing failure.
2. Generate the MCP server in Python. Default to FastMCP unless the user has a strong preference. Wire each tool to a function that calls the source (Snowflake connector, SAP RFC, internal REST, whatever).
3. Add OAuth in front. The MCP spec carries an `Authorization: Bearer ...` header through; your server validates the token against the IdP introspection endpoint.
4. Deploy behind HTTPS — Cloud Run is the typical target because it gives you HTTPS, IAM, and autoscaling out of the box.
5. Register in the Cloud Console: **Gemini Enterprise → Data stores → Create data store → Custom MCP Server**. Fill in the MCP URL, Authorization URL, Token URL, Client ID/Secret, scopes.
6. After creation, open the datastore → **Actions → Reload custom actions**. This triggers `tools/list`. Have the user select the tools to enable.
7. Attach the datastore to a Gemini Enterprise app.

Detailed MCP server scaffold, OAuth wiring, FastMCP example, and Cloud Run deployment recipe: see **`references/mcp-server.md`**.

## Path B: Custom Connector → Discovery Engine datastore

An ingestion connector is a three-stage pipeline — **Fetch → Transform → Sync** — that pushes `Document` payloads into a Discovery Engine datastore. The datastore is then attached to a Gemini Enterprise app, and Gemini grounds answers in it with citations and enforces ACLs at query time.

**Key API surface** (Python SDK = `google-cloud-discoveryengine`):

- `DataStoreServiceClient.create_data_store(...)` with `acl_enabled=True` and an `identity_mapping_store` reference if you need non-Google identities.
- `IdentityMappingStoreServiceClient.create_identity_mapping_store(...)` + `import_identity_mappings(...)` to map external usernames/IDs to Google subjects.
- `DocumentServiceClient.import_documents(...)` with either:
  - `InlineSource` — for development and small batches (incremental reconciliation only).
  - `GcsSource` pointing at JSONL — for production (supports `FULL` and `INCREMENTAL` reconciliation).

**Document payload shape** — every doc needs:

- `id` — stable, derived from the source primary key (this is what makes upserts work).
- `content.raw_bytes` (or `mime_type` + URI) — the text or file body.
- `struct_data` — metadata you want filterable/facetable (owner, tags, timestamps, source URL).
- `acl_info.readers` — list of principals (`user_id`, `group_id`, `external_entity_id`, or `idp_wide: true` for public).

**Scaffold steps** — when the user asks you to build one, do roughly this in order:

1. Map the source's record shape to `Document` fields. Force the user to commit to a stable ID derivation early (e.g. `f"{table}:{primary_key}"`) — every subsequent sync depends on it.
2. Decide the ACL model. If the source uses non-Google identities (employee IDs, internal usernames), you need an Identity Mapping Store. If everything is already Google Workspace email, skip the IMS and use `user_id`/`group_id` directly.
3. Stand up the GCP scaffolding: enable the Discovery Engine API, create a service account with the Discovery Engine Editor role + GCS write on the staging bucket, create the Identity Mapping Store (if needed), create the datastore with `acl_enabled=True`.
4. Write the connector code. For small/dev volumes, start with inline imports. For anything that will go to production, write JSONL to GCS and use `GcsSource` — it's the only path that supports `FULL` reconciliation (needed for clean re-syncs) and it scales 1000× better.
5. Schedule it. Cloud Scheduler → Cloud Run job is the conventional pattern. First run is `FULL`; subsequent runs are `INCREMENTAL` with `MAX(updated_at)` watermarking. Handle deletes via `PurgeDocuments` or by leaving missing IDs out of a FULL re-sync.
6. Attach the datastore to a Gemini Enterprise app and validate that an end user sees only what their ACLs permit.

Detailed Python SDK examples (datastore creation, IMS, inline + GCS import, ACLs, scheduling) live in **`references/ingestion-connector.md`**.

**Hard limits to plan around** (these matter when scoping):

- GCS import: max 100 files per request (or 100,000 if `dataSchema=content`).
- Per-file size: 2GB, or 100MB with `dataSchema=content`.
- Inline imports are incremental-only — you can't do clean replacements without going through GCS.

If the user has more than ~100k documents OR needs sub-hour freshness OR needs to update only ACLs without re-ingesting content, the naive Python script won't hold up. Send them to **`references/enterprise-scale.md`**, which covers the BigQuery-backed CDC pattern (5-table architecture, `MERGE`-based delta detection, External Object Tables for unstructured data, the Patch API for metadata-only updates, `PurgeDocuments` for batch deletes). That pattern benchmarks at ~36k docs/sec — single-Python-script connectors top out around 10–15 docs/sec.

## Source-specific recipes

For sources users ask about often, there's a per-source page with the specifics — connection libraries, common ACL mapping pitfalls, and which pattern (MCP vs. ingestion) usually wins:

- Snowflake: `references/source-recipes/snowflake.md`
- SAP: `references/source-recipes/sap.md`

If the user asks about a source not in this list, default to the decision tree at the top of this file, then borrow the closest recipe as a template. Don't make up source-specific details — if you don't know how the source authenticates or paginates, ask.

## Common pitfalls

A few things that consistently go wrong; flag them proactively if relevant:

- **Picking ingestion when MCP was right.** Replicating a transactional database into a search index is almost never what the user wants. If the source is queried in real time today, MCP is probably the answer.
- **Forgetting `acl_enabled=True` on the datastore.** It can only be set at creation. If you forget, you have to recreate the datastore and re-import everything.
- **Using random UUIDs as document IDs.** Every sync creates duplicates. IDs must be stable and derived from the source's primary key.
- **Not using an Identity Mapping Store for external identities.** Without it, ACLs reference identities Discovery Engine can't resolve, and ACL enforcement silently fails open or closed depending on settings.
- **Trying to do a clean replacement with inline imports.** Inline is incremental-only — go through GCS for `FULL` reconciliation.
- **Standing up the MCP server on HTTP or with a self-signed cert.** Gemini's StreamableHTTP client will refuse to connect.
- **Building a connector before confirming the source isn't already natively supported.** The native connector library grows; check it first.

## When to ask vs. when to just build

If the user gives you a source name and says "build the connector," confirm three things before writing code:

1. Which path (MCP vs. ingestion) — propose one with reasoning, then confirm.
2. The ACL model (Google identities only, or do we need an IMS?).
3. Where the connector will run (Cloud Run, GKE, on-prem?). This determines auth (workload identity vs. service account key) and networking.

Don't ask things you can derive — if the source is a Snowflake warehouse and the user said "let agents query our sales data," you already know it's MCP, you can propose `run_query` as the tool, and you can confirm rather than re-elicit.
