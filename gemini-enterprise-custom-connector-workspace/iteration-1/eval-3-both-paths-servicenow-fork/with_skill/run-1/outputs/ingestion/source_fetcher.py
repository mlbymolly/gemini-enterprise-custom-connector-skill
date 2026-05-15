# source_fetcher.py — paginated KB article reads from the ServiceNow fork
import os
import requests
from typing import Iterator

INSTANCE = os.environ["SNOW_INSTANCE_URL"]  # e.g. https://fork.example.com
USER = os.environ["SNOW_USER"]
PWD = os.environ["SNOW_PASSWORD"]
# Prefer OAuth client credentials in production; basic auth shown for brevity.

PAGE_SIZE = 200


def fetch_kb_articles(since_iso: str | None) -> Iterator[dict]:
    """Yield KB articles updated since the watermark.

    Args:
        since_iso: e.g. '2026-05-15 00:00:00'. Pass None for full sync.
    """
    sysparm_query = "workflow_state=published"
    if since_iso:
        sysparm_query += f"^sys_updated_on>={since_iso}"

    offset = 0
    while True:
        r = requests.get(
            f"{INSTANCE}/api/now/table/kb_knowledge",
            params={
                "sysparm_query": sysparm_query,
                "sysparm_limit": PAGE_SIZE,
                "sysparm_offset": offset,
                "sysparm_fields": (
                    "sys_id,number,short_description,text,kb_category,"
                    "workflow_state,sys_updated_on,view_count,"
                    "can_read_user_criteria,kb_knowledge_base"
                ),
                "sysparm_display_value": "false",
            },
            auth=(USER, PWD),
            headers={"Accept": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        rows = r.json().get("result", [])
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < PAGE_SIZE:
            return
        offset += PAGE_SIZE
