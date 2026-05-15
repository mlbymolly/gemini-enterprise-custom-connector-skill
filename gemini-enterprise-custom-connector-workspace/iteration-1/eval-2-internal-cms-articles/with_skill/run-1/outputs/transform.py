"""CMS article record -> discoveryengine.Document.

Stable ID rule (per SKILL.md "Common pitfalls"): IDs MUST be derived from the
source primary key so that re-syncs upsert rather than duplicate.

ACLs:
- `reader_users` -> internal CMS usernames/emails. We assume these are NOT Google
  Workspace identities (this is an internal CMS), so they go through the Identity
  Mapping Store with the `external_user:` prefix.
- `reader_groups` -> internal CMS group names. Same treatment, `external_group:` prefix.
- `is_public=True` -> idp_wide:true (any signed-in user in the IdP).
"""

from __future__ import annotations

import re
from html import unescape

from google.cloud import discoveryengine_v1 as discoveryengine

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    """Very small HTML-to-text cleaner.

    For richer extraction (preserving structure, lists, headings), swap this for
    BeautifulSoup or trafilatura. Keeping it dependency-light here.
    """
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    text = unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def to_document(record: dict) -> discoveryengine.Document:
    # Stable document id derived from the CMS primary key.
    doc_id = f"cms_article:{record['id']}"

    body_text = _strip_html(record.get("body_html", ""))

    struct = {
        "title": record.get("title", ""),
        "source_url": record.get("url", ""),
        "author": record.get("author_email", ""),
        "tags": record.get("tags", []),
        "updated_at": record["updated_at"].isoformat(),
    }

    # Build the reader principals list.
    readers: list[dict] = []
    if record.get("is_public"):
        readers = [{"idp_wide": True}]
    else:
        for email in record.get("reader_users", []):
            # External CMS user identities resolved by the Identity Mapping Store.
            readers.append({"user_id": f"external_user:{email}"})
        for group in record.get("reader_groups", []):
            readers.append({"group_id": f"external_group:{group}"})

    # Default-deny if no readers given. (Discovery Engine's behavior with an
    # empty readers list is "no one can read"; we surface this with an explicit
    # marker so it's easy to grep for in logs.)
    if not readers:
        readers = [{"user_id": "external_user:__no_readers_sentinel__"}]

    return discoveryengine.Document(
        id=doc_id,
        struct_data=struct,
        content=discoveryengine.Document.Content(
            raw_bytes=body_text.encode("utf-8"),
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
