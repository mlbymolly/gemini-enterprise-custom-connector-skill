"""MCP server exposing Snowflake sales-warehouse tools to Gemini Enterprise.

Transport: StreamableHTTP on /mcp (the only transport Gemini Enterprise accepts).
Auth: OAuth bearer token forwarded by Gemini; validated against the IdP per request.
Snowflake auth: service-account key-pair (model 1 from the Snowflake recipe).
"""

from fastmcp import FastMCP

from auth import validate_bearer_token
from source_client import SnowflakeClient

mcp = FastMCP("snowflake-sales-connector")


@mcp.tool()
def list_tables(schema: str = "SALES") -> dict:
    """List tables and views available in a Snowflake schema in the ANALYTICS database.

    Use this first when the agent needs to discover what sales data exists before
    writing a query. Returns table names, kinds (TABLE/VIEW), and row counts.

    Args:
        schema: Snowflake schema name inside the ANALYTICS database. Defaults to SALES.
    """
    user = validate_bearer_token()
    return SnowflakeClient(user).list_tables(schema=schema)


@mcp.tool()
def describe_table(table: str) -> dict:
    """Return column names, data types, and a few sample values for a Snowflake table.

    Use this before run_query so the agent knows exact column names and types.
    Accepts either SCHEMA.TABLE or DATABASE.SCHEMA.TABLE; defaults to the
    ANALYTICS database when the database is omitted.

    Args:
        table: Table identifier, e.g. "SALES.ORDERS" or "ANALYTICS.SALES.ORDERS".
    """
    user = validate_bearer_token()
    return SnowflakeClient(user).describe_table(table)


@mcp.tool()
def run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only SELECT against the Snowflake sales warehouse and return rows.

    Only SELECT and WITH...SELECT statements are accepted; DDL and DML (INSERT,
    UPDATE, DELETE, MERGE, CREATE, DROP, ALTER, GRANT, COPY, TRUNCATE, CALL) are
    rejected before being sent to Snowflake. The server always injects a LIMIT
    based on max_rows and applies a 60-second statement timeout.

    Args:
        sql: A single SELECT (or WITH ... SELECT) statement. Multi-statement
            scripts and non-SELECT statements are rejected.
        max_rows: Maximum rows to return. Server caps at 1000.
    """
    user = validate_bearer_token()
    return SnowflakeClient(user).run_query(sql=sql, max_rows=min(max_rows, 1000))


@mcp.tool()
def get_record(table: str, primary_key: str) -> dict:
    """Fetch a single sales record by its primary key.

    Use this for "look up order 12345" style questions where the agent already
    has an exact ID. For aggregations, filters, or joins, use run_query instead.

    Args:
        table: Table identifier (SCHEMA.TABLE), e.g. "SALES.ORDERS".
        primary_key: The primary key value of the row to fetch.
    """
    user = validate_bearer_token()
    return SnowflakeClient(user).get_record(table=table, primary_key=primary_key)


if __name__ == "__main__":
    # StreamableHTTP on /mcp. SSE is NOT supported by Gemini Enterprise.
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8080,
        path="/mcp",
    )
