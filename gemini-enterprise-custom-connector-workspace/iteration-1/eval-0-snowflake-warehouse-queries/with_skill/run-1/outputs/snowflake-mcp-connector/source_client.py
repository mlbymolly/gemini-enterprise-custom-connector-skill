"""Snowflake client used by the MCP tools.

Auth model: service account with a key pair (Snowflake recipe model 1).
The private key PEM is loaded from Google Secret Manager so it never lives on
disk or in env vars. All ACL enforcement is expected to be implemented inside
Snowflake (row-access policies, role grants).

Guardrails baked in here:
- run_query is SELECT-only (parsed with sqlglot, not regex).
- LIMIT is always injected/wrapped.
- STATEMENT_TIMEOUT_IN_SECONDS = 60 set at the session level.
- Warehouse should be configured with AUTO_SUSPEND = 60 outside this code.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import snowflake.connector
import sqlglot
import sqlglot.expressions as sg_exp
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from google.cloud import secretmanager

SF_ACCOUNT = os.environ["SNOWFLAKE_ACCOUNT"]
SF_USER = os.environ["SNOWFLAKE_USER"]
SF_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "AGENT_WH")
SF_DATABASE = os.environ.get("SNOWFLAKE_DATABASE", "ANALYTICS")
SF_ROLE = os.environ.get("SNOWFLAKE_ROLE", "AGENT_READER")
SF_PK_SECRET = os.environ["SNOWFLAKE_PRIVATE_KEY_SECRET"]  # full resource name
SF_PK_PASSPHRASE_SECRET = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE_SECRET")

STATEMENT_TIMEOUT_SECONDS = 60
HARD_ROW_CAP = 1000

# DML / DDL keywords we never want to see in run_query.
FORBIDDEN_STATEMENT_TYPES = {
    sg_exp.Insert,
    sg_exp.Update,
    sg_exp.Delete,
    sg_exp.Merge,
    sg_exp.Create,
    sg_exp.Drop,
    sg_exp.Alter,
    sg_exp.TruncateTable,
    sg_exp.Command,  # COPY, GRANT, CALL, etc. land here in sqlglot
}


@lru_cache(maxsize=1)
def _load_private_key() -> bytes:
    """Pull the PEM-encoded private key from Secret Manager, return DER bytes."""
    client = secretmanager.SecretManagerServiceClient()
    pem = client.access_secret_version(name=SF_PK_SECRET).payload.data

    passphrase: bytes | None = None
    if SF_PK_PASSPHRASE_SECRET:
        passphrase = client.access_secret_version(
            name=SF_PK_PASSPHRASE_SECRET
        ).payload.data

    pk = serialization.load_pem_private_key(
        pem, password=passphrase, backend=default_backend()
    )
    return pk.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _validate_select_only(sql: str) -> sg_exp.Expression:
    """Parse the SQL with sqlglot; reject anything that isn't a single SELECT/WITH.

    Returns the parsed expression so callers can re-render it with a LIMIT.
    """
    try:
        statements = sqlglot.parse(sql, read="snowflake")
    except sqlglot.errors.ParseError as exc:
        raise ValueError(f"could not parse SQL: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise ValueError("only a single statement is allowed")

    stmt = statements[0]
    for forbidden in FORBIDDEN_STATEMENT_TYPES:
        if stmt.find(forbidden) is not None:
            raise ValueError(
                f"only SELECT statements are allowed (found {forbidden.__name__})"
            )

    # The top-level must be SELECT or a WITH wrapping a SELECT.
    top = stmt
    if isinstance(top, sg_exp.With):
        top = top.this
    if not isinstance(top, sg_exp.Select):
        raise ValueError("only SELECT (or WITH ... SELECT) statements are allowed")
    return stmt


def _inject_limit(parsed: sg_exp.Expression, max_rows: int) -> str:
    """Wrap the parsed statement in a LIMIT max_rows clause and re-render."""
    capped = min(max_rows, HARD_ROW_CAP)
    # `limit()` replaces any existing LIMIT; if existing LIMIT is smaller it would
    # be overridden upward, so take the minimum to be safe.
    existing_limit = parsed.args.get("limit")
    if existing_limit and isinstance(existing_limit, sg_exp.Limit):
        try:
            existing_value = int(existing_limit.expression.this)
            capped = min(capped, existing_value)
        except (AttributeError, ValueError, TypeError):
            pass
    limited = parsed.limit(capped)
    return limited.sql(dialect="snowflake")


class SnowflakeClient:
    """Thin wrapper around the Snowflake connector used by the MCP tools."""

    def __init__(self, user_claims: dict[str, Any]):
        # user_claims is currently used only for audit/logging. If you switch to
        # OAuth-passthrough (recipe model 2), use claims["sub"] or similar to
        # impersonate the user in Snowflake.
        self.user_claims = user_claims

    def _connect(self) -> snowflake.connector.SnowflakeConnection:
        conn = snowflake.connector.connect(
            account=SF_ACCOUNT,
            user=SF_USER,
            private_key=_load_private_key(),
            warehouse=SF_WAREHOUSE,
            database=SF_DATABASE,
            role=SF_ROLE,
            session_parameters={
                "STATEMENT_TIMEOUT_IN_SECONDS": STATEMENT_TIMEOUT_SECONDS,
                # Belt-and-braces: tell Snowflake this session must not write.
                "TRANSACTION_DEFAULT_ISOLATION_LEVEL": "READ COMMITTED",
            },
            client_session_keep_alive=False,
        )
        return conn

    # ---- Tool implementations -------------------------------------------------

    def list_tables(self, schema: str) -> dict:
        sql = """
            SELECT table_name, table_type, row_count
            FROM information_schema.tables
            WHERE table_schema = %(schema)s
            ORDER BY table_name
        """
        with self._connect() as conn:
            with conn.cursor(snowflake.connector.DictCursor) as cur:
                cur.execute(sql, {"schema": schema.upper()})
                rows = cur.fetchall()
        return {"schema": schema.upper(), "tables": rows}

    def describe_table(self, table: str) -> dict:
        parts = table.split(".")
        if len(parts) == 2:
            schema, table_name = parts
            database = SF_DATABASE
        elif len(parts) == 3:
            database, schema, table_name = parts
        else:
            raise ValueError("table must be SCHEMA.TABLE or DATABASE.SCHEMA.TABLE")

        fqn = f"{database}.{schema}.{table_name}"
        cols_sql = f"""
            SELECT column_name, data_type, is_nullable, comment
            FROM {database}.information_schema.columns
            WHERE table_schema = %(schema)s AND table_name = %(table)s
            ORDER BY ordinal_position
        """
        sample_sql = f"SELECT * FROM {fqn} LIMIT 5"

        with self._connect() as conn:
            with conn.cursor(snowflake.connector.DictCursor) as cur:
                cur.execute(
                    cols_sql,
                    {"schema": schema.upper(), "table": table_name.upper()},
                )
                columns = cur.fetchall()
                cur.execute(sample_sql)
                sample = cur.fetchall()
        return {"table": fqn, "columns": columns, "sample_rows": sample}

    def run_query(self, sql: str, max_rows: int) -> dict:
        parsed = _validate_select_only(sql)
        bounded_sql = _inject_limit(parsed, max_rows)
        with self._connect() as conn:
            with conn.cursor(snowflake.connector.DictCursor) as cur:
                cur.execute(bounded_sql)
                rows = cur.fetchall()
                columns = [c[0] for c in cur.description] if cur.description else []
        return {
            "executed_sql": bounded_sql,
            "row_count": len(rows),
            "columns": columns,
            "rows": rows,
        }

    def get_record(self, table: str, primary_key: str) -> dict:
        parts = table.split(".")
        if len(parts) == 2:
            schema, table_name = parts
            database = SF_DATABASE
        elif len(parts) == 3:
            database, schema, table_name = parts
        else:
            raise ValueError("table must be SCHEMA.TABLE or DATABASE.SCHEMA.TABLE")
        fqn = f"{database}.{schema}.{table_name}"

        # Discover the PK column from information_schema rather than trusting input.
        pk_sql = f"""
            SELECT column_name
            FROM {database}.information_schema.table_constraints tc
            JOIN {database}.information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema    = kcu.table_schema
             AND tc.table_name      = kcu.table_name
            WHERE tc.table_schema = %(schema)s
              AND tc.table_name   = %(table)s
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    pk_sql,
                    {"schema": schema.upper(), "table": table_name.upper()},
                )
                pk_cols = [r[0] for r in cur.fetchall()]
                if not pk_cols:
                    raise ValueError(f"no primary key declared on {fqn}")
                if len(pk_cols) > 1:
                    raise ValueError(
                        f"composite primary key on {fqn}; use run_query for this"
                    )

                pk_col = pk_cols[0]
                lookup_sql = (
                    f"SELECT * FROM {fqn} WHERE {pk_col} = %(pk)s LIMIT 1"
                )
                with conn.cursor(snowflake.connector.DictCursor) as dcur:
                    dcur.execute(lookup_sql, {"pk": primary_key})
                    rows = dcur.fetchall()
        return {"table": fqn, "primary_key_column": pk_col, "row": rows[0] if rows else None}
