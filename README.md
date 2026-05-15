# custom-connectors

Reference implementations and scaffolding for connecting non-native data sources to **Gemini Enterprise**.

Gemini Enterprise ships a broad library of native connectors (Salesforce, ServiceNow, SharePoint, Jira, Confluence, Drive, Slack, GitHub, etc.). When a source isn't in that library, two extension points fill the gap — and this repo provides production-quality examples of both.

## Extension paths

| Path | Pattern | Best for |
|---|---|---|
| **Custom MCP Server** | Live tool calling at query time | Real-time lookups, write actions, ad-hoc SQL |
| **Custom Connector → Discovery Engine** | Fetch → Transform → Sync pipeline | Grounded search, RAG, citations over a periodically-refreshed snapshot |

Use **MCP** when the agent needs the source's current state or needs to write back to it.  
Use **Ingestion** when a snapshot is acceptable and you want answers with citations grounded across the full corpus.  
You can run both for the same source — MCP for live queries, ingestion for the bulk read corpus.

## Repository layout

```
custom-connectors/
│
├── databricks-gemini/              ← Production connector: Databricks ↔ Gemini Enterprise
│   ├── mcp-server/                 Custom MCP Server — live SQL via Cloud Run
│   ├── ingestion-connector/        Ingestion connector — UC volumes & tables → Discovery Engine
│   ├── README.md                   Architecture, decision matrix, config reference
│   └── SETUP.md                    End-to-end deploy walkthrough
│
├── gemini-enterprise-custom-connector/    ← Skill, references, and evals for building connectors
│   ├── SKILL.md                    Oz skill — decision tree, scaffold steps, pitfalls
│   ├── references/                 Deep-dive guides (MCP server, ingestion, enterprise scale, source recipes)
│   ├── scripts/                    Utility scripts
│   └── evals/                      Skill evaluation cases
│
├── gemini-enterprise-custom-connector-workspace/   ← Active development workspace
│   └── iteration-1/
│
└── gemini-enterprise-custom-connector.skill        ← Packaged skill artifact
```

## Getting started

### Databricks connector (working reference implementation)

See [`databricks-gemini/README.md`](./databricks-gemini/README.md) for architecture detail and [`databricks-gemini/SETUP.md`](./databricks-gemini/SETUP.md) for a full deploy walkthrough. The high-level order:

1. **Databricks** — create a service principal, generate an OAuth M2M client secret, grant warehouse + Unity Catalog permissions.
2. **GCP** — enable APIs, create a GCS staging bucket and Firestore database, override the org policy that blocks Custom MCP datastores, create service accounts.
3. **Secret Manager** — store the Databricks SP client ID/secret and IdP client secret.
4. **Discovery Engine** — create the Identity Mapping Store and datastore with `acl_enabled=True` (cannot be changed after creation).
5. **MCP server** — deploy to Cloud Run, register as a Custom MCP Server datastore in the Gemini Enterprise console.
6. **Ingestion connector** — deploy as a Cloud Run Job, run the initial `FULL` import, schedule hourly incrementals.
7. **Attach both datastores** to your Gemini Enterprise app.

### Building a connector for a new source

1. Read [`gemini-enterprise-custom-connector/SKILL.md`](./gemini-enterprise-custom-connector/SKILL.md) — it covers the MCP vs. ingestion decision tree, scaffold steps, and common pitfalls.
2. Browse `gemini-enterprise-custom-connector/references/` for deep-dive guides on the Python SDK, OAuth wiring, ACL configuration, and enterprise-scale patterns.
3. Use `databricks-gemini/` as a working reference to adapt from.

## Key design decisions to make before writing code

1. **MCP or ingestion?** — Confirm before scaffolding. Wrong choice means a re-architecture later.
2. **ACL model** — Google identities only, or do you need an Identity Mapping Store for external subjects?
3. **Where does the connector run?** — Cloud Run (most common), GKE, or on-prem. This determines auth (Workload Identity vs. service account key) and networking.
4. **Scale** — The single-Python-script ingestion pattern tops out at ~10–15 docs/sec. For >100k documents or sub-hour freshness, use the BigQuery CDC pattern described in `references/enterprise-scale.md`.

## Common pitfalls

- Forgetting `acl_enabled=True` at datastore creation — it cannot be added later.
- Using non-stable document IDs — every sync creates duplicates; IDs must be derived from the source's primary key.
- Using inline imports for full re-syncs — inline is incremental-only; use GCS source for `FULL` reconciliation.
- Deploying the MCP server on HTTP or with a self-signed cert — Gemini's StreamableHTTP client will refuse to connect.
- Building a connector before checking whether the source is already natively supported.
