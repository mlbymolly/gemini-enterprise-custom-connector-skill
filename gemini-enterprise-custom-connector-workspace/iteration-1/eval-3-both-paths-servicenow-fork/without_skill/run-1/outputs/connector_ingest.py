"""
KB ingestion connector for a forked ServiceNow instance -> Gemini Enterprise.

Pulls published knowledge articles from your fork's Table API and pushes them
into a Discovery Engine data store as `Document` objects, with ACLs.

Schedule this on Cloud Scheduler -> Cloud Run job, e.g. every 15 minutes.

Env vars expected:
  PROJECT_ID           - GCP project ID
  LOCATION             - e.g. "global"
  DATA_STORE_ID        - existing Discovery Engine data store ID (with acl_enabled=True)
  SN_FORK_BASE_URL     - https://your-fork.example.com
  SN_USER / SN_PASS    - basic-auth creds, OR set SN_BEARER for OAuth bearer
  LAST_RUN_ISO         - optional, ISO8601 timestamp; defaults to 1970-01-01
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Iterable

import httpx
from google.cloud import discoveryengine_v1
from google.cloud.discoveryengine_v1 import Document, ImportDocumentsRequest


PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
DATA_STORE_ID = os.environ["DATA_STORE_ID"]

SN_BASE = os.environ["SN_FORK_BASE_URL"].rstrip("/")
SN_BEARER = os.environ.get("SN_BEARER")
SN_AUTH = None if SN_BEARER else (os.environ["SN_USER"], os.environ["SN_PASS"])

LAST_RUN = os.environ.get("LAST_RUN_ISO", "1970-01-01T00:00:00Z")


# ---------- Fork-side: pull published KB articles ----------

def fetch_published_articles(updated_since_iso: str) -> Iterable[dict]:
    """Yields raw KB article dicts from the forked ServiceNow Table API."""
    headers = {"Accept": "application/json"}
    if SN_BEARER:
        headers["Authorization"] = f"Bearer {SN_BEARER}"

    url = f"{SN_BASE}/api/now/table/kb_knowledge"
    # Adjust the query to your fork's schema. This is the stock ServiceNow shape.
    params = {
        "sysparm_query": (
            f"workflow_state=published^sys_updated_on>={updated_since_iso}"
        ),
        "sysparm_display_value": "true",
        "sysparm_limit": "200",
        "sysparm_offset": "0",
    }

    offset = 0
    while True:
        params["sysparm_offset"] = str(offset)
        r = httpx.get(url, headers=headers, params=params, auth=SN_AUTH, timeout=60)
        r.raise_for_status()
        batch = r.json().get("result", [])
        if not batch:
            return
        for row in batch:
            yield row
        if len(batch) < int(params["sysparm_limit"]):
            return
        offset += len(batch)


# ---------- Transform: raw -> Discovery Engine Document ----------

def to_discovery_document(article: dict) -> Document:
    sys_id = article["sys_id"]
    title = article.get("short_description") or article.get("title") or "Untitled"
    body_html = article.get("text") or article.get("article_body") or ""
    kb_number = article.get("number", "")

    metadata = {
        "title": title,
        "kb_number": kb_number,
        "category": article.get("kb_category", ""),
        "knowledge_base": article.get("kb_knowledge_base", ""),
        "updated_at": article.get("sys_updated_on", ""),
        "url": f"{SN_BASE}/kb_view.do?sys_kb_id={sys_id}",
    }

    # Map fork-side roles to Google or external groups.
    # If the article is org-wide, set idp_wide=True instead of listing principals.
    readers = _build_acl_for(article)

    return Document(
        id=f"kb_{sys_id}",
        schema_id="default_schema",
        content=Document.Content(
            mime_type="text/html",
            raw_bytes=body_html.encode("utf-8"),
        ),
        json_data=json.dumps(metadata),
        acl_info=Document.AclInfo(readers=readers),
    )


def _build_acl_for(article: dict) -> list[Document.AclInfo.AccessRestriction]:
    """Translate fork-side role/group strings into Discovery Engine ACL principals.

    Customize this for your fork. Two common shapes shown below.
    """
    roles_csv: str = article.get("roles", "")
    if not roles_csv:
        # Public article: visible to everyone in the IdP.
        return [Document.AclInfo.AccessRestriction(idp_wide=True)]

    principals = []
    for role in [r.strip() for r in roles_csv.split(",") if r.strip()]:
        # `external_group:` prefix lets you reference groups via the Identity Mapping Store.
        principals.append(
            Document.AclInfo.AccessRestriction.Principal(
                group_id=f"external_group:{role}"
            )
        )
    return [Document.AclInfo.AccessRestriction(principals=principals)]


# ---------- Push to Discovery Engine ----------

def import_documents(docs: list[Document]) -> None:
    if not docs:
        print("No documents to import.")
        return

    client = discoveryengine_v1.DocumentServiceClient()
    parent = client.branch_path(
        project=PROJECT_ID,
        location=LOCATION,
        data_store=DATA_STORE_ID,
        branch="default_branch",
    )

    request = ImportDocumentsRequest(
        parent=parent,
        inline_source=ImportDocumentsRequest.InlineSource(documents=docs),
        reconciliation_mode=ImportDocumentsRequest.ReconciliationMode.INCREMENTAL,
        id_field="id",
    )
    op = client.import_documents(request=request)
    print(f"Started import LRO: {op.operation.name}")
    # For a job runner you can op.result(timeout=600) and inspect failures.


def main() -> None:
    print(f"Pulling KB articles from {SN_BASE} updated since {LAST_RUN}")
    docs: list[Document] = []
    for raw in fetch_published_articles(LAST_RUN):
        docs.append(to_discovery_document(raw))
        if len(docs) >= 100:
            import_documents(docs)
            docs = []
    import_documents(docs)
    print(f"Done at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
