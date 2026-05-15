import os
import time
from typing import Any

import requests
from fastmcp.server.dependencies import get_http_request

INTROSPECT_URL = os.environ["OAUTH_INTROSPECT_URL"]
CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
ALLOWED_AUD = os.environ.get("OAUTH_EXPECTED_AUDIENCE")

_INTROSPECTION_TIMEOUT = 5


def validate_bearer_token() -> dict[str, Any]:
    req = get_http_request()
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise PermissionError("missing bearer token")
    token = auth.split(None, 1)[1].strip()

    r = requests.post(
        INTROSPECT_URL,
        data={"token": token, "token_type_hint": "access_token"},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=_INTROSPECTION_TIMEOUT,
    )
    r.raise_for_status()
    claims = r.json()

    if not claims.get("active"):
        raise PermissionError("token inactive")

    exp = claims.get("exp")
    if exp is not None and int(exp) < int(time.time()):
        raise PermissionError("token expired")

    if ALLOWED_AUD:
        aud = claims.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud] if aud else []
        if ALLOWED_AUD not in aud_list:
            raise PermissionError("token audience mismatch")

    return claims
