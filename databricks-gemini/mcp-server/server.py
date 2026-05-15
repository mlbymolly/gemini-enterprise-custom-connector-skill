from fastmcp import FastMCP

from auth import validate_bearer_token
import databricks_client as db

mcp = FastMCP("databricks-connector")


@mcp.tool()
def list_tables(catalog: str, schema: str) -> dict:
    """List tables and views in a Databricks Unity Catalog schema.

    Use this when the user asks what data is available, what tables exist,
    or before forming a query so you know the right table names.

    Args:
        catalog: Unity Catalog catalog name (e.g. "main", "analytics").
        schema: Schema name within the catalog (e.g. "sales", "default").

    Returns: {catalog, schema, tables: [{name, type, comment}]}
    """
    validate_bearer_token()
    return db.list_tables(catalog, schema)


@mcp.tool()
def describe_table(catalog: str, schema: str, table: str) -> dict:
    """Return the column schema for a single Databricks table or view.

    Call this before run_query to confirm column names and types. Returns
    column name, Databricks SQL type, nullability, and any column comment.

    Args:
        catalog: Unity Catalog catalog name.
        schema: Schema name within the catalog.
        table: Table or view name within the schema.

    Returns: {table, columns: [{name, type, nullable, comment}]}
    """
    validate_bearer_token()
    return db.describe_table(catalog, schema, table)


@mcp.tool()
def run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only SQL query against the Databricks SQL Warehouse.

    Only SELECT and WITH ... SELECT statements are allowed. DDL (CREATE,
    DROP, ALTER) and DML (INSERT, UPDATE, DELETE, MERGE) are rejected.
    Multiple statements per call are rejected. The server injects a LIMIT
    if one isn't present, capped at 1000 rows regardless of max_rows.

    Args:
        sql: A single SELECT or WITH ... SELECT statement, Databricks SQL dialect.
        max_rows: Maximum rows to return. Capped at 1000.

    Returns: {columns, rows, row_count, truncated, executed_sql}
    """
    validate_bearer_token()
    return db.run_query(sql, max_rows=max_rows)


@mcp.tool()
def get_record(catalog: str, schema: str, table: str, key_column: str, key_value: str) -> dict:
    """Fetch a single row from a Databricks table by primary key.

    Use this for targeted lookups when you already know the table and the
    key value. For range queries or aggregations, use run_query instead.

    Args:
        catalog: Unity Catalog catalog name.
        schema: Schema name within the catalog.
        table: Table name within the schema.
        key_column: Name of the column to filter on (usually the primary key).
        key_value: Value to match. Passed as a parameter, not concatenated.

    Returns: {columns, rows, row_count, truncated, executed_sql}
    """
    validate_bearer_token()
    return db.get_record_by_key(catalog, schema, table, key_column, key_value)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080, path="/mcp")
