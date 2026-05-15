# Reading KB articles AND creating tickets from a ServiceNow fork

Short version: your fork isn't a native connector, so you'll build a **custom connector** — and because you want both **read** (search knowledge articles with citations) and **write** (create tickets), you need **both** Gemini Enterprise extension points, not one. Specifically:

- **Custom Connector → Discovery Engine datastore** ingests your KB articles on a schedule so Gemini can ground answers in them with citations and ACL enforcement.
- **Custom MCP Server** exposes a `create_incident` tool (and any other live ServiceNow operations) that Gemini calls at query time, on behalf of the asking user.

This is the canonical "both paths" deployment the SKILL.md decision tree maps you to:

1. *Current state vs. snapshot?* — KB articles are fine as a snapshot (15 min – 1 hour refresh). Tickets must be current — and creating a ticket is a write, which means MCP regardless.
2. *Write back to the source?* — Yes (create tickets) → MCP.
3. *Grounded answers with citations across other corpora?* — Yes for KB articles → ingestion connector.

So one of each. Below are scaffolds and concrete code for both.

---

## Assumptions I'm making

State these back to me if any are wrong; I've built around them:

- Your fork is "ServiceNow-shaped" — speaks REST, has a `kb_knowledge` table for articles and an `incident` table for tickets, supports OAuth 2.0. If your fork swapped out the auth model or table schema, only `source_client.py` / `source_fetcher.py` change; the rest of the architecture is identical.
- Users authenticate with Google Workspace identities in Gemini Enterprise, but ServiceNow uses its own `sys_id` user records. That means you need an **Identity Mapping Store** to map Workspace emails → ServiceNow `sys_id`s for the KB ACLs.
- Your fork is reachable from Google's public network (or you can put a Cloud Run egress in front of a private endpoint). Private Service Connect / VPC-SC are **not** supported for MCP datastores in preview — if the fork is air-gapped, the MCP path won't work and you'd need a proxy.
- Per the skill: even though native ServiceNow has a connector, **a fork isn't covered by it**. You're building custom. The native ACL/sync semantics aren't free here — you implement them.

---

## Architecture

```
                          ┌─────────────────────────────────────┐
                          │      Gemini Enterprise app          │
                          └───────────────┬─────────────────────┘
                                          │
                       ┌──────────────────┴──────────────────┐
                       │                                     │
              ┌────────▼─────────┐                  ┌────────▼──────────┐
              │ Discovery Engine │                  │  Custom MCP       │
              │  datastore       │                  │  datastore        │
              │  (KB articles,   │                  │  (live tools)     │
              │  acl_enabled)    │                  │                   │
              └────────▲─────────┘                  └────────▲──────────┘
                       │                                     │
       FULL/INCREMENTAL import                  StreamableHTTP + Bearer
                       │                                     │
              ┌────────┴─────────┐                  ┌────────┴──────────┐
              │ Ingestion        │                  │ MCP server on     │
              │ connector        │                  │ Cloud Run         │
              │ (Cloud Run Job + │                  │ tools:            │
              │  Cloud Scheduler)│                  │  - create_incident│
              └────────▲─────────┘                  │  - get_incident   │
                       │                            │  - search_kb_live │
                       │                            └────────▲──────────┘
                       │                                     │
                       └───────────────┬─────────────────────┘
                                       │
                            ┌──────────▼────────────┐
                            │  Your ServiceNow fork │
                            │  (REST API)           │
                            └───────────────────────┘
```

Two completely separate runtimes, one shared backend. They're decoupled — failure in one doesn't break the other.

---

## Path A: Ingestion connector for KB articles

Maps the skill's Path B scaffold to your ServiceNow fork.

### Document model

- **Stable ID**: `f"snow_kb:{article['sys_id']}"` — `sys_id` is ServiceNow's primary key, won't change on edits.
- **Body**: `article['text']` (the article body), stripped of HTML if needed.
- **struct_data**: `title`, `short_description`, `kb_category`, `workflow_state`, `view_count`, `sys_updated_on`, `source_url` (the deep link back to the article in ServiceNow).
- **ACL readers**: ServiceNow KB articles are typically scoped by `can_read_user_criteria` or by `kb_knowledge_base` membership. Map those to:
  - `external_group:<criteria_sys_id>` for criteria-based access (needs IMS entries mapping criteria → Workspace groups).
  - `idp_wide: True` for the special "public KB" case (anyone in your IdP).

### Watermarking

ServiceNow exposes `sys_updated_on`. Query `sys_updated_on>=<last_watermark>` on every incremental run. Persist the watermark in Firestore or GCS.

### Code: `source_fetcher.py`

```python
# source_fetcher.py — paginated KB article reads from the ServiceNow fork
import os, requests
from typing import Iterator

INSTANCE = os.environ["SNOW_INSTANCE_URL"]  # e.g. https://fork.example.com
USER = os.environ["SNOW_USER"]
PWD = os.environ["SNOW_PASSWORD"]
# Prefer OAuth client credentials in production; basic auth shown for brevity.

PAGE_SIZE = 200

def fetch_kb_articles(since_iso: str | None) -> Iterator[dict]:
    """Yield KB articles updated since the watermark.

    Args:
        since_iso: e.g. '2026-05-15 00:00:00'. Pass None for full sync.
    """
    sysparm_query = "workflow_state=published"
    if since_iso:
        sysparm_query += f"^sys_updated_on>={since_iso}"

    offset = 0
    while True:
        r = requests.get(
            f"{INSTANCE}/api/now/table/kb_knowledge",
            params={
                "sysparm_query": sysparm_query,
                "sysparm_limit": PAGE_SIZE,
                "sysparm_offset": offset,
                "sysparm_fields": (
                    "sys_id,number,short_description,text,kb_category,"
                    "workflow_state,sys_updated_on,view_count,"
                    "can_read_user_criteria,kb_knowledge_base"
                ),
                "sysparm_display_value": "false",
            },
            auth=(USER, PWD),
            headers={"Accept": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        rows = r.json().get("result", [])
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < PAGE_SIZE:
            return
        offset += PAGE_SIZE
```

### Code: `transform.py`

```python
# transform.py — ServiceNow KB row -> discoveryengine.Document
import re
from google.cloud import discoveryengine_v1 as discoveryengine

INSTANCE = __import__("os").environ["SNOW_INSTANCE_URL"]

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").strip()

def to_document(article: dict) -> discoveryengine.Document:
    doc_id = f"snow_kb:{article['sys_id']}"
    body = _strip_html(article.get("text", ""))

    struct = {
        "title": article.get("short_description", ""),
        "kb_number": article.get("number", ""),
        "category": article.get("kb_category", ""),
        "workflow_state": article.get("workflow_state", ""),
        "view_count": int(article.get("view_count") or 0),
        "updated_at": article.get("sys_updated_on", ""),
        "source_url": (
            f"{INSTANCE}/kb_view.do?sysparm_article={article.get('number','')}"
        ),
    }

    # Build readers. NOTE: criteria sys_ids must already exist in the IMS
    # as external_group entries mapped to Workspace groups.
    readers = []
    criteria = (article.get("can_read_user_criteria") or "").split(",")
    for c in filter(None, (x.strip() for x in criteria)):
        readers.append({"group_id": f"external_group:{c}"})

    # Fallback: if no criteria, treat as IdP-wide (your fork's "all employees"
    # KB base equivalent). Tune this — default-deny is safer than default-open.
    if not readers:
        readers = [{"idp_wide": True}]

    return discoveryengine.Document(
        id=doc_id,
        struct_data=struct,
        content=discoveryengine.Document.Content(
            raw_bytes=body.encode("utf-8"),
            mime_type="text/plain",
        ),
        acl_info=discoveryengine.Document.AclInfo(
            readers=[
                discoveryengine.Document.AclInfo.AccessRestriction(
                    principals=[discoveryengine.Principal(**p) for p in readers],
                )
            ],
        ),
    )
```

### Code: `connector.py` (Cloud Run Job entry point)

```python
# connector.py — orchestrate fetch -> transform -> GCS JSONL -> import
import os, json, datetime, sys
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import storage
from source_fetcher import fetch_kb_articles
from transform import to_document

PROJECT_ID   = os.environ["GCP_PROJECT"]
LOCATION     = os.environ.get("DE_LOCATION", "global")
DATASTORE_ID = os.environ["DE_DATASTORE_ID"]
BUCKET       = os.environ["STAGING_BUCKET"]
WATERMARK_BLOB = "watermarks/snow_kb_last_run.txt"

def _read_watermark() -> str | None:
    blob = storage.Client().bucket(BUCKET).blob(WATERMARK_BLOB)
    return blob.download_as_text() if blob.exists() else None

def _write_watermark(ts: str) -> None:
    storage.Client().bucket(BUCKET).blob(WATERMARK_BLOB).upload_from_string(ts)

def main(mode: str = "INCREMENTAL"):
    since = None if mode == "FULL" else _read_watermark()
    docs, count = [], 0
    for row in fetch_kb_articles(since):
        docs.append(to_document(row))
        count += 1

    if not docs:
        print("no new articles; nothing to import")
        return

    # Write JSONL to GCS — production path.
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    blob_name = f"snow_kb/{ts}_{mode.lower()}.jsonl"
    jsonl = "\n".join(
        discoveryengine.Document.to_json(d, indent=None) for d in docs
    ) + "\n"
    storage.Client().bucket(BUCKET).blob(blob_name).upload_from_string(
        jsonl, content_type="application/json"
    )
    gcs_uri = f"gs://{BUCKET}/{blob_name}"

    # Import.
    client = discoveryengine.DocumentServiceClient()
    parent = client.branch_path(PROJECT_ID, LOCATION, DATASTORE_ID, "default_branch")
    recon = (
        discoveryengine.ImportDocumentsRequest.ReconciliationMode.FULL
        if mode == "FULL"
        else discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL
    )
    op = client.import_documents(
        request=discoveryengine.ImportDocumentsRequest(
            parent=parent,
            gcs_source=discoveryengine.GcsSource(input_uris=[gcs_uri]),
            reconciliation_mode=recon,
        )
    )
    print(f"imported {count} docs via {gcs_uri}; op={op.operation.name}")

    # Advance the watermark only after import is submitted.
    _write_watermark(datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

if __name__ == "__main__":
    main(mode=sys.argv[1] if len(sys.argv) > 1 else "INCREMENTAL")
```

### One-time setup (run before the connector ever runs)

You need: the **Identity Mapping Store**, the **datastore with `acl_enabled=True`**, the GCS bucket, and the service account. See `references/ingestion-connector.md` in the skill for the exact `create_ims()` + `create_datastore()` snippets. The key things specific to ServiceNow:

- Populate the IMS with one entry per ServiceNow user criteria you care about: `external_identity="<criteria_sys_id>"`, `group_id="<workspace_group_id>"`. Asymmetry to remember: bare sys_id when importing, prefixed (`external_group:<sys_id>`) when referenced in a document ACL.
- `acl_enabled=True` must be set on the datastore at creation — **cannot be added later**.

### Schedule

- Cloud Scheduler → Cloud Run Job, every 30 minutes, runs `connector.py INCREMENTAL`.
- Weekly: `connector.py FULL` to catch drift, retires, and ACL changes.
- Handle deletes via `PurgeDocuments` or by relying on the weekly FULL re-sync to drop missing IDs.

---

## Path B: MCP server for live writes (create_incident, etc.)

Maps the skill's Path A scaffold. This one is short because tools are the whole point.

### Tool surface

Pick the verb-led set Gemini will route to. Tool descriptions are the *only* thing routing the agent — write them like documentation:

- `create_incident(short_description, description, urgency, caller_email)` — create a new incident on behalf of the asking user.
- `get_incident(number)` — fetch the current state of an existing incident by number (`INC0010234`).
- `update_incident(number, work_notes, state)` — append work notes / change state.
- `search_kb_live(query, limit)` — optional; useful when the agent wants article state newer than the ingestion watermark.

You don't need `search_kb_live` if the ingestion connector's freshness is good enough — the datastore already covers reads. Add it only if you've measured that 30-minute lag is a problem.

### Code: `server.py` (FastMCP + StreamableHTTP)

```python
# server.py — Custom MCP server for the ServiceNow fork
from fastmcp import FastMCP
from auth import validate_bearer_token
from source_client import ServiceNowClient

mcp = FastMCP("servicenow-fork-mcp")

@mcp.tool()
def create_incident(
    short_description: str,
    description: str,
    urgency: int = 3,
    caller_email: str | None = None,
) -> dict:
    """Create a new incident (ticket) in ServiceNow.

    Use this when the user asks to "open a ticket", "file a ticket",
    "create an incident", or report an issue. The incident is created on
    behalf of the asking user; pass their email as `caller_email` if known.

    Args:
        short_description: One-line summary shown in lists. Required.
        description: Full details, reproduction steps, context.
        urgency: 1 (high) – 3 (low). Default 3.
        caller_email: Workspace email of the caller; defaults to the
                      authenticated user from the bearer token.

    Returns:
        dict with `number` (e.g. "INC0012345"), `sys_id`, `state`, `url`.
    """
    user = validate_bearer_token()
    caller = caller_email or user.get("email")
    return ServiceNowClient(user).create_incident(
        short_description=short_description,
        description=description,
        urgency=urgency,
        caller_email=caller,
    )

@mcp.tool()
def get_incident(number: str) -> dict:
    """Fetch the current state of an incident by its number.

    Args:
        number: ServiceNow incident number, e.g. "INC0012345".

    Returns:
        Full incident record including state, assigned_to, work_notes,
        resolution_notes, sys_updated_on.
    """
    user = validate_bearer_token()
    return ServiceNowClient(user).get_incident(number)

@mcp.tool()
def update_incident(
    number: str,
    work_notes: str | None = None,
    state: str | None = None,
) -> dict:
    """Update an existing incident: append work notes and/or change state.

    Args:
        number: Incident number, e.g. "INC0012345".
        work_notes: Text to append to the work_notes journal.
        state: New state, one of "new", "in_progress", "resolved", "closed".
    """
    user = validate_bearer_token()
    return ServiceNowClient(user).update_incident(
        number, work_notes=work_notes, state=state
    )

if __name__ == "__main__":
    # StreamableHTTP is the ONLY transport Gemini Enterprise accepts.
    # Do NOT switch to SSE — tools/list will return 0 tools.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080, path="/mcp")
```

### Code: `source_client.py`

```python
# source_client.py — thin wrapper over the ServiceNow fork's REST API
import os, requests

INSTANCE = os.environ["SNOW_INSTANCE_URL"]
STATE_MAP = {"new": "1", "in_progress": "2", "resolved": "6", "closed": "7"}

class ServiceNowClient:
    def __init__(self, user_claims: dict):
        # In production: exchange the bearer token via on-behalf-of flow,
        # OR use a service account with impersonation via x-snow-user header.
        self._user_email = user_claims.get("email")
        self._auth = (os.environ["SNOW_SVC_USER"], os.environ["SNOW_SVC_PASSWORD"])

    def create_incident(self, *, short_description, description, urgency, caller_email):
        r = requests.post(
            f"{INSTANCE}/api/now/table/incident",
            json={
                "short_description": short_description,
                "description": description,
                "urgency": str(urgency),
                "caller_id": caller_email,  # fork-specific: may need sys_id lookup
            },
            auth=self._auth,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        rec = r.json()["result"]
        return {
            "number": rec["number"],
            "sys_id": rec["sys_id"],
            "state": rec["state"],
            "url": f"{INSTANCE}/nav_to.do?uri=incident.do?sys_id={rec['sys_id']}",
        }

    def get_incident(self, number: str) -> dict:
        r = requests.get(
            f"{INSTANCE}/api/now/table/incident",
            params={"sysparm_query": f"number={number}", "sysparm_limit": 1},
            auth=self._auth,
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json().get("result", [])
        if not rows:
            return {"error": f"no incident {number}"}
        return rows[0]

    def update_incident(self, number, *, work_notes=None, state=None) -> dict:
        rec = self.get_incident(number)
        if "error" in rec:
            return rec
        payload = {}
        if work_notes:
            payload["work_notes"] = work_notes
        if state:
            payload["state"] = STATE_MAP.get(state, state)
        r = requests.patch(
            f"{INSTANCE}/api/now/table/incident/{rec['sys_id']}",
            json=payload, auth=self._auth, timeout=30,
        )
        r.raise_for_status()
        return r.json()["result"]
```

### Code: `auth.py`

Standard bearer-token validator from the skill — validate against your IdP's introspection endpoint or by verifying a JWT. (See `references/mcp-server.md` for the canonical snippet; it's source-agnostic and works as-is.) If your fork has its own OAuth server, you'd point `OAUTH_INTROSPECT_URL` at *that* — not at ServiceNow's. Gemini's bearer must be from the IdP you registered as the Authorization URL.

### Deploy + register

1. `gcloud run deploy servicenow-fork-mcp --source . --region us-central1 --allow-unauthenticated --set-env-vars "OAUTH_INTROSPECT_URL=...,OAUTH_CLIENT_ID=...,OAUTH_CLIENT_SECRET=...,SNOW_INSTANCE_URL=...,SNOW_SVC_USER=...,SNOW_SVC_PASSWORD=..."`
2. Register the redirect URI `https://vertexaisearch.cloud.google.com/oauth-redirect` in your IdP (exact, no trailing slash).
3. GCP prereqs: override the org policy that blocks custom MCP datastores, and grant the admin "Discovery Engine Editor".
4. **Console → Gemini Enterprise → Data stores → Create data store → Custom MCP Server**. Fill in MCP URL, Auth URL, Token URL, Client ID/Secret, scopes (include `offline_access` so refresh tokens are issued).
5. After creation: **Actions → Reload custom actions** to trigger `tools/list`. Select the tools to enable.

---

## Wiring both to one Gemini Enterprise app

This is the bit people miss. The two datastores are independent — you attach **both** to the same app:

1. **Gemini Enterprise → Apps → (your app) → Data stores → Add** — add the ingestion datastore (KB articles).
2. Same app → **Add** again — add the custom MCP datastore (live tools).
3. Save.

Now the agent can answer "how do I reset my VPN?" by grounding in the indexed KB *and* "open me a ticket about VPN flakiness" by calling `create_incident` — in the same conversation, with the same Workspace identity threading through both.

---

## Things that will bite you (specific to this setup)

- **ACL silently fails open or closed if the IMS isn't populated correctly.** Test by giving a single article a single-user reader and confirming a different user can't see it.
- **`acl_enabled=True` is a creation-time setting.** Forgot it? Recreate the datastore and re-ingest.
- **Inline import is incremental-only.** Go straight to the GCS JSONL pattern shown above — don't waste time on inline beyond initial dev.
- **Tool descriptions are the routing signal.** "Create a ticket" written vaguely will cause Gemini to skip the tool and answer in plain text instead. Each docstring should start with a verb and name the resource.
- **StreamableHTTP only.** If you're starting from an MCP example that uses SSE, switch transports first or `tools/list` will return zero.
- **Don't use UUIDs as document IDs.** `snow_kb:{sys_id}` is stable across re-runs; `uuid4()` makes duplicates on every sync.
- **Private Service Connect / VPC-SC aren't supported for MCP datastores in preview.** If your fork is fully private, you need a Cloud Run service in the same VPC fronting it. The MCP endpoint itself must be reachable from Google's public network.
- **Native-vs-fork:** the native ServiceNow connector won't work against a fork because the fork's auth, table schema, or REST surface diverges enough that the native plugin breaks. Confirm before going custom that this is actually true for your fork (the skill flags this as a common pitfall — "Building a connector before confirming the source isn't already natively supported"). If your fork is just `service-now.com` with custom tables, the native connector + a few custom tool calls in MCP can sometimes be cheaper than building both from scratch.

---

## File layout you'd end up with

```
servicenow-fork-connector/
├── ingestion/                       # Path A
│   ├── connector.py
│   ├── source_fetcher.py
│   ├── transform.py
│   ├── identity_mapping.py
│   ├── infra/create_datastore.py
│   └── Dockerfile
└── mcp/                             # Path B
    ├── server.py
    ├── source_client.py
    ├── auth.py
    └── Dockerfile
```

Each subdirectory deploys as its own Cloud Run target (a Job for ingestion, a Service for MCP). They share nothing at runtime — that's the point.

---

## What I'd want to confirm before writing any more code

1. **OAuth IdP for the MCP path** — is it Okta, Azure AD, Google, or your fork's own OAuth server? That determines `auth.py`'s exact shape.
2. **ACL model on KB articles in your fork** — `can_read_user_criteria`, `kb_knowledge_base` membership, or something custom? The transform's `readers` derivation depends on this and is the #1 thing that goes wrong silently.
3. **Where you want to run this** — Cloud Run is the default in the skill. If you need it on GKE or on-prem, the auth wiring (workload identity vs. SA key) changes.

If you want, I can fill in the IMS-setup script, the `create_datastore.py`, and the Dockerfiles — they're the same as the skill's reference scaffolds with the env-var names swapped in. Say the word.
