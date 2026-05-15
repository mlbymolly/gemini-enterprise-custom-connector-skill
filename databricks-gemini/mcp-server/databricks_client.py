import os
from functools import lru_cache

from databricks import sql
from databricks.sdk.core import Config, oauth_service_principal
from databricks.sdk import WorkspaceClient

from sql_safety import assert_read_only, cap_rows

DATABRICKS_HOST = os.environ["DATABRICKS_HOST"]
DATABRICKS_HTTP_PATH = os.environ["DATABRICKS_HTTP_PATH"]
DATABRICKS_CLIENT_ID = os.environ["DATABRICKS_CLIENT_ID"]
DATABRICKS_CLIENT_SECRET = os.environ["DATABRICKS_CLIENT_SECRET"]

STATEMENT_TIMEOUT_SECONDS = int(os.environ.get("DATABRICKS_STMT_TIMEOUT", "60"))
ABS_MAX_ROWS = int(os.environ.get("DATABRICKS_ABS_MAX_ROWS", "1000"))


def _sp_credential_provider():
    config = Config(
        host=f"https://{DATABRICKS_HOST}",
        client_id=DATABRICKS_CLIENT_ID,
        client_secret=DATABRICKS_CLIENT_SECRET,
    )
    return oauth_service_principal(config)


@lru_cache(maxsize=1)
def _workspace_client() -> WorkspaceClient:
    return WorkspaceClient(
        host=f"https://{DATABRICKS_HOST}",
        client_id=DATABRICKS_CLIENT_ID,
        client_secret=DATABRICKS_CLIENT_SECRET,
    )


def _connect():
    return sql.connect(
        server_hostname=DATABRICKS_HOST,
        http_path=DATABRICKS_HTTP_PATH,
        credentials_provider=_sp_credential_provider,
        session_configuration={
            "STATEMENT_TIMEOUT": str(STATEMENT_TIMEOUT_SECONDS),
        },
    )


def run_query(query: str, max_rows: int) -> dict:
    assert_read_only(query)
    effective_max = max(1, min(max_rows, ABS_MAX_ROWS))
    capped = cap_rows(query, effective_max)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(capped)
        columns = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(effective_max)

    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": len(rows) == effective_max,
        "executed_sql": capped,
    }


def list_tables(catalog: str, schema: str) -> dict:
    sql_text = (
        "SELECT table_name, table_type, comment "
        "FROM system.information_schema.tables "
        "WHERE table_catalog = %(c)s AND table_schema = %(s)s "
        "ORDER BY table_name"
    )
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql_text, {"c": catalog, "s": schema})
        rows = cur.fetchall()
    return {
        "catalog": catalog,
        "schema": schema,
        "tables": [
            {"name": r[0], "type": r[1], "comment": r[2]} for r in rows
        ],
    }


def describe_table(catalog: str, schema: str, table: str) -> dict:
    sql_text = (
        "SELECT column_name, data_type, is_nullable, comment "
        "FROM system.information_schema.columns "
        "WHERE table_catalog = %(c)s AND table_schema = %(s)s AND table_name = %(t)s "
        "ORDER BY ordinal_position"
    )
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql_text, {"c": catalog, "s": schema, "t": table})
        rows = cur.fetchall()
    return {
        "table": f"{catalog}.{schema}.{table}",
        "columns": [
            {
                "name": r[0],
                "type": r[1],
                "nullable": r[2] == "YES",
                "comment": r[3],
            }
            for r in rows
        ],
    }


_IDENT_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _safe_ident(name: str, kind: str) -> str:
    if not name or not all(c in _IDENT_OK for c in name):
        raise ValueError(f"invalid {kind} identifier: {name!r}")
    return f"`{name}`"


def get_record_by_key(
    catalog: str, schema: str, table: str, key_column: str, key_value: str
) -> dict:
    qualified = ".".join(
        _safe_ident(p, k)
        for p, k in [(catalog, "catalog"), (schema, "schema"), (table, "table")]
    )
    col = _safe_ident(key_column, "column")
    sql_text = f"SELECT * FROM {qualified} WHERE {col} = %(v)s LIMIT 1"

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql_text, {"v": key_value})
        columns = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(1)

    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": False,
        "executed_sql": sql_text,
    }


def get_workspace() -> WorkspaceClient:
    return _workspace_client()
