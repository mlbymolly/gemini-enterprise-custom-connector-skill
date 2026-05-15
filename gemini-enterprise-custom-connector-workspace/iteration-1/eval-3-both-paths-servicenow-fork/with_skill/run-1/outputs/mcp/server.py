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
