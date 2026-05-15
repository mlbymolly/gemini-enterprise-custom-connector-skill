# auth.py — OAuth bearer token validation for the MCP server.
#
# The Gemini Enterprise StreamableHTTP client forwards the user's IdP-issued
# access token in the Authorization header. Validate against the IdP that you
# registered as the Authorization/Token URL when creating the MCP datastore
# in the Gemini Enterprise console. This is NOT the ServiceNow fork's OAuth
# server — it's your corporate IdP (Okta / Azure AD / Google / etc.).
import os
import requests
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
