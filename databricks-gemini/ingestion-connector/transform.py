from google.cloud import discoveryengine_v1 as discoveryengine

from databricks_fetcher import FileRecord, TableRowRecord


def _principals(
    users: list[str], groups: list[str], public: bool
) -> list[discoveryengine.Principal]:
    if public:
        return [discoveryengine.Principal(idp_wide=True)]
    out: list[discoveryengine.Principal] = []
    for u in users:
        out.append(discoveryengine.Principal(user_id=u))
    for g in groups:
        # external_group: prefix lets the IMS resolve Databricks groups to
        # Google subjects at query time. If the group name is already a
        # Google Workspace group email, drop the prefix.
        principal_id = g if "@" in g else f"external_group:{g}"
        out.append(discoveryengine.Principal(group_id=principal_id))
    return out


def _acl_info(
    users: list[str], groups: list[str], public: bool
) -> discoveryengine.Document.AclInfo:
    return discoveryengine.Document.AclInfo(
        readers=[
            discoveryengine.Document.AclInfo.AccessRestriction(
                principals=_principals(users, groups, public),
            )
        ]
    )


def file_record_to_document(r: FileRecord) -> discoveryengine.Document:
    doc_id = f"databricks_volume:{r.volume_path}".replace("/", "_")[:1024]

    return discoveryengine.Document(
        id=doc_id,
        content=discoveryengine.Document.Content(
            raw_bytes=r.body,
            mime_type=r.mime_type,
        ),
        struct_data={
            "title": r.name,
            "source": "databricks_uc_volume",
            "volume_path": r.volume_path,
            "updated_at": r.updated_at.isoformat(),
        },
        acl_info=_acl_info(r.readers_users, r.readers_groups, r.public),
    )


def table_row_to_document(r: TableRowRecord) -> discoveryengine.Document:
    doc_id = f"databricks_row:{r.catalog}.{r.schema}.{r.table}:{r.primary_key}"

    return discoveryengine.Document(
        id=doc_id,
        content=discoveryengine.Document.Content(
            raw_bytes=r.rendered_text.encode("utf-8"),
            mime_type="text/plain",
        ),
        struct_data={
            "title": f"{r.table} row {r.primary_key}",
            "source": "databricks_uc_table",
            "catalog": r.catalog,
            "schema": r.schema,
            "table": r.table,
            "primary_key": r.primary_key,
            "updated_at": r.updated_at.isoformat(),
            **{k: _struct_safe(v) for k, v in r.struct_data.items()},
        },
        acl_info=_acl_info(r.readers_users, r.readers_groups, r.public),
    )


def _struct_safe(v):
    # struct_data has to round-trip through google.protobuf.Struct, which
    # only accepts JSON scalars + nested dict/list.
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)
