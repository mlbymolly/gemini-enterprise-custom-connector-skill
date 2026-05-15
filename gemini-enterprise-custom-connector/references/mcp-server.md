# MCP server scaffold for Gemini Enterprise

This is the detailed reference for **Path A** in SKILL.md. Read SKILL.md first to confirm MCP is the right path.

## Architecture

```
Gemini Enterprise  ──OAuth──▶  IdP (Okta / Azure AD / GCP / etc.)
        │
        │  StreamableHTTP (Bearer token)
        ▼
   Your MCP server  ──▶  Source system (Snowflake / SAP / API)
   (Cloud Run, HTTPS)
```

Gemini stores the OAuth refresh token per user, exchanges it for an access token, and attaches it to every MCP request as `Authorization: Bearer ...`. Your server validates the token against the IdP and then makes the source call on the user's behalf.

## Project layout

A minimal Python MCP server for Gemini Enterprise:

```
my-mcp-connector/
├── pyproject.toml
├── server.py            # MCP server + tool definitions
├── auth.py              # OAuth token validation
├── source_client.py     # The actual connection to Snowflake/SAP/etc.
├── Dockerfile           # For Cloud Run deployment
└── README.md
```

## server.py — FastMCP with StreamableHTTP

FastMCP supports StreamableHTTP transport, which is the only one Gemini Enterprise accepts.

```python
from fastmcp import FastMCP
from auth import validate_bearer_token
from source_client import SourceClient

mcp = FastMCP("my-connector")

@mcp.tool()
def run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only SQL query against the warehouse and return rows.

    Args:
        sql: SELECT statement. DDL and DML are rejected.
        max_rows: Maximum rows to return (server caps at 1000).
    """
    user = validate_bearer_token()  # raises 401 if invalid
    return SourceClient(user).query(sql, max_rows=min(max_rows, 1000))

@mcp.tool()
def get_record(table: str, record_id: str) -> dict:
    """Fetch a single record by primary key."""
    user = validate_bearer_token()
    return SourceClient(user).get(table, record_id)

if __name__ == "__main__":
    # StreamableHTTP transport on /mcp — Gemini Enterprise REQUIRES this.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080, path="/mcp")
```

Tool description quality is the routing signal — write each docstring as if it's the only thing Gemini will see (because it is). Be specific about inputs, outputs, and what the tool will NOT do.

## auth.py — OAuth token validation

Gemini forwards the user's OAuth access token. Your server validates it against the IdP's introspection endpoint (or by verifying a JWT signature):

```python
import os, requests
from contextvars import ContextVar
from fastmcp.server.dependencies import get_http_request

INTROSPECT_URL = os.environ["OAUTH_INTROSPECT_URL"]
CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]

def validate_bearer_token() -> dict:
    req = get_http_request()
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise PermissionError("missing bearer token")
    token = auth.split(None, 1)[1]
    r = requests.post(
        INTROSPECT_URL,
        data={"token": token},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=5,
    )
    r.raise_for_status()
    claims = r.json()
    if not claims.get("active"):
        raise PermissionError("token inactive")
    return claims  # contains sub, email, scope, etc.
```

For JWT-style tokens (Okta, Azure AD), prefer signature verification + claims check over introspection — it's faster and doesn't require a network hop per call.

## Dockerfile — Cloud Run target

```Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY . .
ENV PORT=8080
CMD ["python", "server.py"]
```

## Deploy to Cloud Run

```bash
gcloud run deploy my-mcp-connector \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "OAUTH_INTROSPECT_URL=...,OAUTH_CLIENT_ID=...,OAUTH_CLIENT_SECRET=..."
```

`--allow-unauthenticated` is fine because *your* OAuth check is the auth layer. Don't add Cloud Run IAM on top — Gemini Enterprise's caller isn't a Google identity it can be granted.

## OAuth setup in your IdP

Register Gemini Enterprise as an OAuth client:

- **Redirect URI**: `https://vertexaisearch.cloud.google.com/oauth-redirect` (exact, no trailing slash)
- **Grant types**: `authorization_code`, `refresh_token`
- **Scopes**: Define whatever your tools need. Always include `offline_access` so Gemini can refresh.
- **Token endpoint auth**: `client_secret_post` is the safe default.

## Register the MCP server in Gemini Enterprise

GCP prereqs first (the user has to do these — they require org-level permissions):

1. Override the org policy constraint that blocks custom MCP datastores. The constraint name is `constraints/discoveryengine.allowedCustomMcpDatastores` (or similar — check the current name in the console).
2. Grant the admin "Discovery Engine Editor" on the project.

Then in the console:

1. **Gemini Enterprise → Data stores → Create data store → Custom MCP Server**.
2. Fill in:
   - **MCP Server URL**: `https://your-cloud-run-url/mcp`
   - **Authorization URL**: IdP base auth URL (no query params)
   - **Token URL**: IdP token URL
   - **Client ID / Secret**: from the OAuth app registration
   - **Scopes**: space-separated, include `offline_access`
3. Complete the OAuth login flow with the **Login** button.
4. Pick a multi-region location and name the datastore.
5. After creation: **Datastore → Actions → Reload custom actions**. This calls `tools/list` against your server. Select the tools to enable.
6. Attach the datastore to your Gemini Enterprise app.

## Networking limits (as of preview)

- **Private Service Connect**: not supported.
- **VPC Service Controls**: not supported.
- The MCP server URL must be reachable from Google's public network.

If the source is behind a firewall with no public ingress, you have two options:
- Run the MCP server in front of the firewall (e.g., on Cloud Run in the same VPC, with Serverless VPC Access connecting to the private backend). The MCP server itself stays public; only its outbound call hits private space.
- Switch to an ingestion connector — that pattern can run entirely inside the VPC and push data out via the Discovery Engine API.

## Testing locally

You can test the MCP server with the MCP inspector before deploying:

```bash
npx @modelcontextprotocol/inspector python server.py
```

This lets you call `tools/list` and individual tools without going through Gemini Enterprise. Catch tool description / schema mistakes here, not after deploy.

## Common failure modes

- **`tools/list` returns 0 tools after "Reload custom actions"**: server is using SSE transport, not StreamableHTTP. Switch to `transport="streamable-http"`.
- **OAuth redirect fails**: IdP redirect URI doesn't match `https://vertexaisearch.cloud.google.com/oauth-redirect` exactly (trailing slash, http vs https, wrong subdomain).
- **Tools work in inspector but Gemini "can't find" them**: tool description is too vague — Gemini routes on description, not name alone. Rewrite each docstring to start with a verb and name the exact resource it touches.
- **Random `401` mid-conversation**: access token expired and your IdP isn't issuing refresh tokens. Add `offline_access` to scopes.
