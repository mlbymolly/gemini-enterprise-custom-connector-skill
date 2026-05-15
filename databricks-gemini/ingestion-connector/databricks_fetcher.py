"""Pluggable fetchers for the two common Databricks doc sources:

1. Unity Catalog volume files (PDFs, markdown, text, Office docs).
2. Reference table rows (small slow-changing tables, rendered to text).

Each fetcher yields raw record dicts. Transform.py turns them into Documents.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from databricks import sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config, oauth_service_principal

DATABRICKS_HOST = os.environ["DATABRICKS_HOST"]
DATABRICKS_CLIENT_ID = os.environ["DATABRICKS_CLIENT_ID"]
DATABRICKS_CLIENT_SECRET = os.environ["DATABRICKS_CLIENT_SECRET"]


@dataclass
class FileRecord:
    source: str = "uc_volume"
    volume_path: str = ""      # /Volumes/<catalog>/<schema>/<volume>/...
    name: str = ""
    body: bytes = b""
    mime_type: str = "application/octet-stream"
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    readers_users: list[str] = field(default_factory=list)
    readers_groups: list[str] = field(default_factory=list)
    public: bool = False


@dataclass
class TableRowRecord:
    source: str = "uc_table"
    catalog: str = ""
    schema: str = ""
    table: str = ""
    primary_key: str = ""
    rendered_text: str = ""
    struct_data: dict = field(default_factory=dict)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    readers_users: list[str] = field(default_factory=list)
    readers_groups: list[str] = field(default_factory=list)
    public: bool = False


def _workspace() -> WorkspaceClient:
    return WorkspaceClient(
        host=f"https://{DATABRICKS_HOST}",
        client_id=DATABRICKS_CLIENT_ID,
        client_secret=DATABRICKS_CLIENT_SECRET,
    )


def _sql_connect(http_path: str):
    def cred():
        return oauth_service_principal(
            Config(
                host=f"https://{DATABRICKS_HOST}",
                client_id=DATABRICKS_CLIENT_ID,
                client_secret=DATABRICKS_CLIENT_SECRET,
            )
        )

    return sql.connect(
        server_hostname=DATABRICKS_HOST,
        http_path=http_path,
        credentials_provider=cred,
    )


_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json",
    ".csv": "text/csv",
}


def _guess_mime(name: str) -> str:
    name = name.lower()
    for ext, mime in _MIME_BY_EXT.items():
        if name.endswith(ext):
            return mime
    return "application/octet-stream"


def fetch_volume_files(
    volume_root: str,
    since: datetime | None = None,
    default_readers_groups: list[str] | None = None,
) -> Iterator[FileRecord]:
    """Walk a UC volume and yield one FileRecord per file.

    `volume_root` is the full UC path, e.g. /Volumes/main/docs/policies.
    `since` filters by modification time when provided (incremental mode).
    `default_readers_groups` is a fallback ACL applied to every file — the
    typical pattern is to give the volume's reader group access to all docs
    indexed from it.
    """
    w = _workspace()
    stack = [volume_root]

    while stack:
        path = stack.pop()
        for entry in w.files.list_directory_contents(directory_path=path):
            if entry.is_directory:
                stack.append(entry.path)
                continue
            mtime = datetime.fromtimestamp(
                (entry.modification_time or 0) / 1000, tz=timezone.utc
            )
            if since is not None and mtime <= since:
                continue
            body = w.files.download(file_path=entry.path).contents.read()
            yield FileRecord(
                volume_path=entry.path,
                name=entry.name or entry.path.rsplit("/", 1)[-1],
                body=body,
                mime_type=_guess_mime(entry.name or entry.path),
                updated_at=mtime,
                readers_groups=list(default_readers_groups or []),
            )


def fetch_table_rows(
    http_path: str,
    catalog: str,
    schema: str,
    table: str,
    primary_key: str,
    columns_for_text: list[str],
    columns_for_metadata: list[str] | None = None,
    watermark_column: str | None = None,
    since: datetime | None = None,
    default_readers_groups: list[str] | None = None,
) -> Iterator[TableRowRecord]:
    """Stream rows from a Databricks table, one TableRowRecord per row.

    `columns_for_text`: rendered into the document body. Each row becomes
        "col1: value1. col2: value2." — plain prose, which Gemini grounds on.
    `columns_for_metadata`: stored in struct_data, filterable/facetable.
    `watermark_column`/`since`: used for incremental syncs.
    """
    cols = sorted(set(columns_for_text + (columns_for_metadata or []) + [primary_key]))
    if watermark_column:
        cols = sorted(set(cols + [watermark_column]))
    col_list = ", ".join(f"`{c}`" for c in cols)
    qualified = f"`{catalog}`.`{schema}`.`{table}`"

    params: dict = {}
    where = ""
    if watermark_column and since is not None:
        where = f" WHERE `{watermark_column}` > %(since)s"
        params["since"] = since

    sql_text = f"SELECT {col_list} FROM {qualified}{where}"

    with _sql_connect(http_path) as conn, conn.cursor() as cur:
        cur.execute(sql_text, params)
        col_names = [d[0] for d in cur.description]
        for row in cur:
            record = dict(zip(col_names, row))
            text_parts = [
                f"{c}: {record[c]}" for c in columns_for_text if record.get(c) is not None
            ]
            metadata = {
                c: record[c] for c in (columns_for_metadata or []) if c in record
            }
            updated_at = record.get(watermark_column) if watermark_column else None
            if isinstance(updated_at, datetime):
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
            else:
                updated_at = datetime.now(timezone.utc)

            yield TableRowRecord(
                catalog=catalog,
                schema=schema,
                table=table,
                primary_key=str(record[primary_key]),
                rendered_text=". ".join(text_parts) + ".",
                struct_data=metadata,
                updated_at=updated_at,
                readers_groups=list(default_readers_groups or []),
            )
