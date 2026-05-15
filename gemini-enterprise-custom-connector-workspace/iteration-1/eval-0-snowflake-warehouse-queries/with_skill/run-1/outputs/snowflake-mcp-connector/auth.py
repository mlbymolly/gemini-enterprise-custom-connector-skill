"""OAuth bearer token validation for inbound MCP requests from Gemini Enterprise.

Gemini Enterprise forwards the end-user's OAuth access token on every MCP call as
an `Authorization: Bearer ...` header. We validate it against the IdP's
introspection endpoint and return the claims (sub, email, scopes) for downstream
use (e.g. audit logging, per-user routing).

For JWT IdPs (Okta, Azure AD, Google) signature verification is faster than
introspection and avoids a network hop per call. Swap to PyJWT + JWKS for prod
if your IdP supports it.
"""

from __future__ import annotations

import os

import requests
from fastmcp.server.dependencies import get_http_request

INTROSPECT_URL = os.environ["OAUTH_INTROSPECT_URL"]
CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]


class AuthError(PermissionError):
    """Raised when the bearer token is missing or invalid."""


def validate_bearer_token() -> dict:
    """Validate the incoming bearer token and return the IdP claims.

    Raises AuthError (a PermissionError subclass) on any failure. FastMCP
    surfaces this to the caller as a tool error; Gemini Enterprise will treat it
    as a 401-equivalent and prompt the user to re-authenticate.
    """
    req = get_http_request()
    auth_header = req.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    token = auth_header.split(None, 1)[1]

    try:
        resp = requests.post(
            INTROSPECT_URL,
            data={"token": token},
            auth=(CLIENT_ID, CLIENT_SECRET),
            timeout=5,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise AuthError(f"introspection call failed: {exc}") from exc

    claims = resp.json()
    if not claims.get("active"):
        raise AuthError("token inactive")
    return claims
