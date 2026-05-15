"""
Custom MCP server wrapping the forked ServiceNow Table API.

Exposes tools that a Gemini Enterprise custom MCP data store can call so the
assistant can create / update incidents in your fork.

Transport: StreamableHTTP (the only transport Gemini Enterprise custom MCP supports).

Deploy to Cloud Run, then register the public URL as a custom MCP data store in
Gemini Enterprise. Configure OAuth 2.0 so Gemini Enterprise can authenticate to it.

Env vars expected:
  SN_FORK_BASE_URL     - https://your-fork.example.com
  SN_USER / SN_PASS    - basic auth, OR set SN_BEARER for OAuth bearer
"""

from __future__ import annotations

import os
from typing import Optional

import httpx
from fastmcp import FastMCP


SN_BASE = os.environ["SN_FORK_BASE_URL"].rstrip("/")
SN_BEARER = os.environ.get("SN_BEARER")
SN_AUTH = None if SN_BEARER else (os.environ["SN_USER"], os.environ["SN_PASS"])


mcp = FastMCP(
    name="servicenow-fork",
    instructions=(
        "Tools for creating and updating incidents in the company's forked "
        "ServiceNow instance. Use create_incident when a user explicitly asks "
        "to open a ticket or when the knowledge base did not resolve their issue."
    ),
)


def _headers() -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if SN_BEARER:
        h["Authorization"] = f"Bearer {SN_BEARER}"
    return h


@mcp.tool()
def create_incident(
    short_description: str,
    description: str,
    caller_email: str,
    urgency: str = "3",
    category: str = "inquiry",
    assignment_group: Optional[str] = None,
) -> dict:
    """Create an incident ticket in the forked ServiceNow instance.

    Args:
        short_description: One-line summary, max ~160 chars.
        description: Full free-text problem description from the user.
        caller_email: Email of the user the ticket is opened on behalf of.
        urgency: "1" High, "2" Medium, "3" Low. Default "3".
        category: Incident category (e.g. "network", "hardware"). Default "inquiry".
        assignment_group: Optional assignment group sys_id or name.

    Returns:
        Dict with `number` (e.g. INC0010001) and `sys_id` so the assistant can
        confirm the ticket to the user.
    """
    payload = {
        "short_description": short_description,
        "description": description,
        "caller_id": caller_email,
        "urgency": urgency,
        "category": category,
    }
    if assignment_group:
        payload["assignment_group"] = assignment_group

    r = httpx.post(
        f"{SN_BASE}/api/now/table/incident",
        headers=_headers(),
        auth=SN_AUTH,
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    result = r.json()["result"]
    return {
        "number": result["number"],
        "sys_id": result["sys_id"],
        "state": result.get("state"),
        "url": f"{SN_BASE}/incident.do?sys_id={result['sys_id']}",
    }


@mcp.tool()
def update_incident(sys_id: str, fields: dict) -> dict:
    """Update an existing incident by sys_id.

    Args:
        sys_id: The incident's sys_id (returned from create_incident).
        fields: Dict of fields to update, e.g. {"urgency": "1", "state": "2"}.
    """
    r = httpx.patch(
        f"{SN_BASE}/api/now/table/incident/{sys_id}",
        headers=_headers(),
        auth=SN_AUTH,
        json=fields,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["result"]


@mcp.tool()
def get_incident(number: str) -> dict:
    """Look up an incident by its human-readable number, e.g. INC0010001."""
    r = httpx.get(
        f"{SN_BASE}/api/now/table/incident",
        headers=_headers(),
        auth=SN_AUTH,
        params={"sysparm_query": f"number={number}", "sysparm_limit": "1"},
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json().get("result", [])
    if not rows:
        return {"found": False}
    row = rows[0]
    return {
        "found": True,
        "number": row["number"],
        "sys_id": row["sys_id"],
        "state": row.get("state"),
        "short_description": row.get("short_description"),
        "url": f"{SN_BASE}/incident.do?sys_id={row['sys_id']}",
    }


if __name__ == "__main__":
    # StreamableHTTP is required by Gemini Enterprise custom MCP data stores.
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )
