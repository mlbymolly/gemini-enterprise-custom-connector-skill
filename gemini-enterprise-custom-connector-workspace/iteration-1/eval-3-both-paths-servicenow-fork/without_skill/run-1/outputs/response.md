# Reading Knowledge Articles AND Creating Tickets in a Forked ServiceNow from a Gemini Enterprise Agent

You're asking about a very common pattern: an agent that combines **retrieval** (knowledge articles) with **action** (creating tickets) — except the native Gemini Enterprise ServiceNow connector doesn't work, because your ServiceNow is a *fork* (a self-hosted or rebranded variant) rather than the SaaS instance that Google's native connector expects.

The good news: Gemini Enterprise gives you the right escape hatches for both halves of this problem. They are intentionally separate primitives, and you'll use a different mechanism for each.

## TL;DR — pick the right primitive for each job

| Need | Native connector works? | What you build instead |
|---|---|---|
| Read knowledge articles (semantic search, grounding) | No — fork isn't recognized | **Custom data connector** that ingests articles into a Discovery Engine data store |
| Create / update tickets (incidents) | No — actions are wired to the native connector | **Custom action** — either via a custom **MCP server** that wraps your fork's REST API, **or** via a custom **ADK agent** with a tool function |

You wire **both** of these into the same Gemini Enterprise app/agent so the model can answer "How do I reset my VPN?" by reading the KB, and "open a ticket for me" by calling the write API — in the same conversation.

## Assumptions I'm making

- Your "fork of ServiceNow" exposes ServiceNow-compatible REST APIs (e.g. `/api/now/table/kb_knowledge`, `/api/now/table/incident`), or at least HTTP endpoints you can call with a bearer token / basic auth. If it doesn't, you'll need to map your fork's endpoints into the patterns shown below — the architecture doesn't change.
- You have admin access to a Gemini Enterprise project and the Discovery Engine / Vertex AI APIs enabled.
- You're OK running a small ingestion job (Cloud Run job / Cloud Scheduler) and either a small MCP server or an ADK agent on Cloud Run / Agent Engine.
- You want this in production, not just a demo, so I'll call out ACLs, refresh, and auth.

---

## Architecture at a glance

```
                       +---------------------------------+
                       |     Gemini Enterprise App       |
                       |  (assistant / custom agent)     |
                       +----------------+----------------+
                                        |
                 ----------------------------------------------
                 |                                            |
        READ PATH (grounding)                       WRITE PATH (actions)
                 |                                            |
   +-------------v-------------+              +---------------v---------------+
   |  Discovery Engine data    |              |  Custom MCP server  OR  ADK   |
   |  store (KB articles)      |              |  agent with a tool function    |
   |                           |              |                                |
   |  Populated by your custom |              |  Calls your fork's REST API:   |
   |  ingestion connector      |              |  POST /api/now/table/incident  |
   +-------------+-------------+              +---------------+----------------+
                 |                                            |
                 |  pulls articles                            |  creates / updates
                 v                                            v
        +----------------+                            +----------------+
        | ServiceNow     |                            | ServiceNow     |
        | fork  (KB API) |                            | fork (Table API)|
        +----------------+                            +----------------+
```

The two paths are independent — that's the key insight. The model doesn't care that they happen to point at the same backend; it sees a search tool and an action tool and picks whichever the user's intent requires.

---

## Part 1 — Read path: a custom connector that ingests KB articles

The Gemini Enterprise native ServiceNow connector won't authenticate against your fork, but the underlying primitives (Discovery Engine data stores) are fully usable on their own. You build a small program that:

1. Pulls knowledge articles from your fork's API (e.g. `GET /api/now/table/kb_knowledge?sysparm_query=workflow_state=published`).
2. Transforms each article into a Discovery Engine `Document` (title, description/content, URI, structured metadata, ACLs).
3. Calls `documents.import` to push them into a data store.
4. Runs on a schedule (Cloud Scheduler -> Cloud Run job) to keep things fresh.

### Document shape

Each KB article becomes a `Document`:

```json
{
  "id": "kb_KB0001234",
  "schemaId": "default_schema",
  "content": {
    "mimeType": "text/html",
    "rawBytes": "<base64 of article HTML/text>"
  },
  "jsonData": "{\"title\":\"How to reset VPN\",\"kb_number\":\"KB0001234\",\"category\":\"Network\",\"updated_at\":\"2026-04-12T14:33:00Z\",\"url\":\"https://your-fork.example.com/kb_view.do?sys_id=...\"}",
  "aclInfo": {
    "readers": [
      { "principals": [
          { "groupId": "external_group:itsm-knowledge-readers" }
        ],
        "idpWide": false
      }
    ]
  }
}
```

Key choices:
- `aclInfo.readers` — if articles are role-restricted in your fork, map those roles to Google groups (or external groups via an **Identity Mapping Store**). If everything is org-wide, set `idpWide: true`.
- Stable IDs (e.g. `kb_<sys_id>`) so re-runs upsert rather than duplicate.
- Use `INCREMENTAL` reconciliation for routine syncs and `FULL` (via GCS staging) when you want deletions to propagate.

### Ingestion connector pseudocode

See `connector_ingest.py` for a runnable starting point. The shape is:

```python
# Pull from your fork
articles = sn_fork.get_published_kb_articles(updated_since=last_run)

# Transform
docs = [to_discovery_document(a) for a in articles]

# Push to Discovery Engine
client = discoveryengine_v1.DocumentServiceClient()
client.import_documents(
    parent=branch,
    inline_source=ImportDocumentsRequest.InlineSource(documents=docs),
    reconciliation_mode=ImportDocumentsRequest.ReconciliationMode.INCREMENTAL,
    id_field="id",
)
```

### Wire the data store into the agent

In Gemini Enterprise console, attach the new data store to your app. The assistant (or a custom agent) will use it for grounded retrieval — citations include the `uri` you put in `jsonData`, so users can click back to your fork's KB view.

---

## Part 2 — Write path: a custom action that creates tickets

The native "ServiceNow actions" feature is bound to the native connector — it won't work for your fork. You have **two equally good options**. Pick based on how the rest of your agent ecosystem is built.

### Option A — Custom MCP server (recommended if you're already on the BYO-MCP path)

Gemini Enterprise supports a **custom MCP server data store** (BYO-MCP). You stand up an MCP server (StreamableHTTP transport — SSE is **not** supported), declare tools like `create_incident` and `update_incident`, and Gemini Enterprise calls them from the assistant.

Skeleton (Python, `fastmcp`):

```python
from fastmcp import FastMCP
import httpx, os

mcp = FastMCP("servicenow-fork")

SN_BASE = os.environ["SN_FORK_BASE_URL"]      # https://your-fork.example.com
SN_AUTH = (os.environ["SN_USER"], os.environ["SN_PASS"])  # or OAuth bearer

@mcp.tool()
def create_incident(
    short_description: str,
    description: str,
    caller_email: str,
    urgency: str = "3",          # 1=High, 2=Medium, 3=Low
    category: str = "inquiry",
) -> dict:
    """Create an incident ticket in ServiceNow (forked instance).

    Returns the new incident number and sys_id so the assistant can confirm to the user.
    """
    payload = {
        "short_description": short_description,
        "description": description,
        "caller_id": caller_email,
        "urgency": urgency,
        "category": category,
    }
    r = httpx.post(f"{SN_BASE}/api/now/table/incident", auth=SN_AUTH, json=payload, timeout=30)
    r.raise_for_status()
    result = r.json()["result"]
    return {"number": result["number"], "sys_id": result["sys_id"]}

@mcp.tool()
def update_incident(sys_id: str, fields: dict) -> dict:
    r = httpx.patch(f"{SN_BASE}/api/now/table/incident/{sys_id}",
                    auth=SN_AUTH, json=fields, timeout=30)
    r.raise_for_status()
    return r.json()["result"]

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
```

Deploy to Cloud Run, then register it in Gemini Enterprise as a custom MCP data store. Configure OAuth 2.0 for Gemini Enterprise -> your MCP server (redirect URL `https://vertexaisearch.cloud.google.com/oauth-redirect`). The admin then explicitly enables each tool — they're disabled by default.

Why MCP: it's the same wrapper you'd use for any other internal tool, so you build one server and add more tools (e.g. `attach_kb_article_to_incident`) over time without touching the agent.

### Option B — Custom ADK agent with tool functions

If you want maximum control over orchestration (e.g. "look up the KB article, then create the ticket and attach the article ID"), build an ADK agent. The tool is just a Python function:

```python
from google.adk.agents import Agent
from google.adk.tools import tool
import httpx, os

SN_BASE = os.environ["SN_FORK_BASE_URL"]
SN_AUTH = (os.environ["SN_USER"], os.environ["SN_PASS"])

@tool
def create_incident(short_description: str, description: str,
                    caller_email: str, urgency: str = "3") -> dict:
    """Create an incident in the forked ServiceNow instance."""
    r = httpx.post(
        f"{SN_BASE}/api/now/table/incident",
        auth=SN_AUTH,
        json={"short_description": short_description,
              "description": description,
              "caller_id": caller_email,
              "urgency": urgency},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["result"]

agent = Agent(
    name="itsm_helper",
    model="gemini-2.5-pro",
    instruction=(
        "You help employees with IT issues. "
        "Always try to answer from the knowledge base first (via the configured "
        "data store). Only call create_incident after you've confirmed with the user "
        "that the KB didn't solve their problem, and after collecting a short "
        "description, longer description, and their email. Always echo back the "
        "ticket number from the tool result."
    ),
    tools=[create_incident],
)
```

Deploy to Vertex AI Agent Engine, then register the agent with your Gemini Enterprise app (console "Add agent -> Custom agent via Agent Platform", or REST `/agents` endpoint). The KB data store from Part 1 is attached to the same app, so the model gets retrieval automatically.

---

## Putting both paths together in one agent

Whichever write-path option you pick, the **same** Gemini Enterprise app gets both:

1. The KB **data store** (from the custom ingestion connector) — used for grounding.
2. The MCP server **or** the custom ADK agent — used for actions.

The model's system prompt / agent instruction is what makes the combined flow feel coherent. A reasonable instruction looks like:

> "Answer IT questions using the connected ServiceNow knowledge base first. Cite the article URL. If the user still needs help, or explicitly asks to open a ticket, call `create_incident` with a short_description, full description, caller email (from the user's identity), and an urgency you infer from their message. Always read the new incident number back to the user."

---

## Auth, ACLs, and identity — don't skip these

- **Reading**: ACLs on the data store control *which user can see which article*. Map your fork's roles/groups to Google groups, or use an **Identity Mapping Store** for non-Google identities.
- **Writing**: The MCP server / ADK tool runs as a service identity by default. If you want the ticket's `caller_id` to be the actual end user (and you should), pass the user's email through. ADK and Gemini Enterprise both surface the signed-in user's email to the agent. For higher-trust writes, configure OAuth 2.0 so the agent acts *as* the user against your fork.
- **Secrets**: Service account / API credentials for your fork belong in Secret Manager, mounted into the Cloud Run / Agent Engine env — never inline.

## Refresh strategy for the KB

- Webhooks from your fork on `kb_knowledge` publish/update events, if it supports them — instant freshness.
- Otherwise, Cloud Scheduler -> Cloud Run job every 15 min, using `sys_updated_on >= last_run` filter.
- Run a periodic **full** sync (e.g. nightly via GCS staging + `FULL` reconciliation) so deletions/retirements propagate.

## Limitations and gotchas to know up-front

- **MCP transport**: StreamableHTTP only. SSE servers will not work.
- **No PSC** for custom MCP data stores in the current preview — your MCP endpoint must be reachable from Google's network (public HTTPS with auth, or via an approved private connectivity option once GA).
- **Custom agent vs custom action**: don't confuse them. A custom ADK agent replaces the assistant; a custom MCP server adds tools the assistant can call. You can do either, not both for the same conversation surface, so pick once.
- **Tools off by default**: after registering a custom MCP server, an admin has to explicitly enable each tool in the console before the agent can call it.
- **ServiceNow native actions are *not* extensible** to point at your fork's URL — they're tied to the native connector. That's why we're going around them.

## Files in this output directory

- `response.md` — this document
- `connector_ingest.py` — runnable skeleton for the KB ingestion connector (Part 1)
- `mcp_server.py` — runnable skeleton for the custom MCP server (Part 2, Option A)
- `adk_agent.py` — runnable skeleton for the custom ADK agent (Part 2, Option B)

## Sources

- [Create custom connector | Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/create-custom-connector)
- [Set up your custom MCP server data store | Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server)
- [Add ServiceNow actions | Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/assistant-actions-servicenow)
- [ServiceNow configuration | Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/servicenow/third-party-config)
- [Register and manage ADK agents hosted on Gemini Enterprise Agent Platform](https://docs.cloud.google.com/gemini/enterprise/docs/register-and-manage-an-adk-agent)
- [Build a Custom Connector for Gemini Enterprise — Sascha Heyer](https://medium.com/google-cloud/build-a-custom-connector-for-gemini-enterprise-ad3aab884645)
- [Override the organization policy for Custom MCP data stores](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/override-constraint-for-custom-mcp-data-stores)
