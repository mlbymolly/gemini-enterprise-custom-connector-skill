# transform.py — ServiceNow KB row -> discoveryengine.Document
import os
import re
from google.cloud import discoveryengine_v1 as discoveryengine

INSTANCE = os.environ["SNOW_INSTANCE_URL"]


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def to_document(article: dict) -> discoveryengine.Document:
    doc_id = f"snow_kb:{article['sys_id']}"
    body = _strip_html(article.get("text", ""))

    struct = {
        "title": article.get("short_description", ""),
        "kb_number": article.get("number", ""),
        "category": article.get("kb_category", ""),
        "workflow_state": article.get("workflow_state", ""),
        "view_count": int(article.get("view_count") or 0),
        "updated_at": article.get("sys_updated_on", ""),
        "source_url": (
            f"{INSTANCE}/kb_view.do?sysparm_article={article.get('number','')}"
        ),
    }

    # Build readers. NOTE: criteria sys_ids must already exist in the IMS
    # as external_group entries mapped to Workspace groups.
    readers = []
    criteria = (article.get("can_read_user_criteria") or "").split(",")
    for c in filter(None, (x.strip() for x in criteria)):
        readers.append({"group_id": f"external_group:{c}"})

    # Fallback: if no criteria, treat as IdP-wide (your fork's "all employees"
    # KB base equivalent). Tune this — default-deny is safer than default-open.
    if not readers:
        readers = [{"idp_wide": True}]

    return discoveryengine.Document(
        id=doc_id,
        struct_data=struct,
        content=discoveryengine.Document.Content(
            raw_bytes=body.encode("utf-8"),
            mime_type="text/plain",
        ),
        acl_info=discoveryengine.Document.AclInfo(
            readers=[
                discoveryengine.Document.AclInfo.AccessRestriction(
                    principals=[discoveryengine.Principal(**p) for p in readers],
                )
            ],
        ),
    )
