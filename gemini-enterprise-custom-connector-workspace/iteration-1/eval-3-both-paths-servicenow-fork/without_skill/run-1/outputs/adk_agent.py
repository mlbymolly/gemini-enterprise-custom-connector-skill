"""
Custom ADK agent for Gemini Enterprise: reads the KB data store (ingested
separately by connector_ingest.py) and creates incidents in the forked
ServiceNow instance via a tool function.

Deploy to Vertex AI Agent Engine, then register the resulting Reasoning Engine
resource with your Gemini Enterprise app (console -> Add agent -> Custom agent
via Agent Platform, or POST /agents on the discoveryengine v1alpha API).

Env vars expected at runtime:
  SN_FORK_BASE_URL  - https://your-fork.example.com
  SN_USER / SN_PASS - basic auth, OR set SN_BEARER for OAuth bearer
"""

from __future__ import annotations

import os
from typing import Optional

import httpx
from google.adk.agents import Agent
from google.adk.tools import tool


SN_BASE = os.environ["SN_FORK_BASE_URL"].rstrip("/")
SN_BEARER = os.environ.get("SN_BEARER")
SN_AUTH = None if SN_BEARER else (os.environ["SN_USER"], os.environ["SN_PASS"])


def _headers() -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if SN_BEARER:
        h["Authorization"] = f"Bearer {SN_BEARER}"
    return h


@tool
def create_incident(
    short_description: str,
    description: str,
    caller_email: str,
    urgency: str = "3",
    category: str = "inquiry",
    assignment_group: Optional[str] = None,
) -> dict:
    """Create an incident in the forked ServiceNow instance.

    Use this only after the knowledge base did not resolve the user's question,
    or when the user explicitly asks to open a ticket.
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
        "url": f"{SN_BASE}/incident.do?sys_id={result['sys_id']}",
    }


@tool
def update_incident(sys_id: str, fields: dict) -> dict:
    """Update an existing incident by sys_id."""
    r = httpx.patch(
        f"{SN_BASE}/api/now/table/incident/{sys_id}",
        headers=_headers(),
        auth=SN_AUTH,
        json=fields,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["result"]


INSTRUCTION = """
You are an IT helpdesk assistant.

1. Always try to answer the user's question using the knowledge base data store
   that's attached to this agent. Cite the article's URL so the user can read more.
2. If the KB does not contain the answer, or if the user explicitly asks to open
   a ticket, gather:
     - a one-line short description,
     - a longer description in the user's own words,
     - their email (use the signed-in user's email if Gemini Enterprise provides it),
     - a sensible urgency (1 High / 2 Medium / 3 Low) based on impact.
   Then call create_incident and read the resulting incident number back to the
   user along with a clickable link.
3. If the user references an existing ticket number (e.g. INC0010001) and asks
   for changes, use update_incident.
4. Never invent ticket numbers or article URLs. Always derive them from tool
   results or retrieved documents.
""".strip()


root_agent = Agent(
    name="itsm_helper",
    model="gemini-2.5-pro",
    instruction=INSTRUCTION,
    tools=[create_incident, update_incident],
)
