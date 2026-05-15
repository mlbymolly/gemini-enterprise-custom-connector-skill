"""
Transform a SuccessFactors PolicyDocument into a discoveryengine.Document
suitable for ingestion into the Gemini Enterprise datastore.

Two design decisions worth flagging:

1. Stable ID derivation. We use `sf:dms:{documentId}`. The SF `documentId` is
   the primary key inside the DMS and survives renames / metadata edits, so
   the same doc on the next sync upserts cleanly instead of duplicating.

2. ACL mapping. SuccessFactors role-based-permission (RBP) groups are external
   identities. We reference them in the document ACL with the
   `external_group:<group_id>` prefix; the Identity Mapping Store resolves
   them to Google identities at query time. Policies marked "all employees"
   become `idp_wide=True` (every authenticated user of the IdP can read).
"""

from __future__ import annotations

from google.cloud import discoveryengine_v1 as discoveryengine

from source_fetcher import PolicyDocument


def to_document(policy: PolicyDocument) -> discoveryengine.Document:
    doc_id = f"sf:dms:{policy.document_id}"

    struct = {
        "title": policy.title,
        "description": policy.description,
        "source_system": "sap_successfactors",
        "doc_type": "hr_policy",
        "source_url": policy.source_url,           # surfaced in citations
        "business_unit": policy.business_unit or "",
        "language": policy.language or "",
        "effective_date": policy.effective_date.isoformat() if policy.effective_date else "",
        "expiration_date": policy.expiration_date.isoformat() if policy.expiration_date else "",
        "updated_at": policy.updated_at.isoformat(),
    }

    # ACL principals.
    if policy.all_employees:
        principals = [discoveryengine.Principal(idp_wide=True)]
    else:
        principals = [
            discoveryengine.Principal(
                # external_group:<rbp_id> — IMS resolves to Google Group(s).
                group_id=f"external_group:{group_id}",
            )
            for group_id in policy.rbp_groups
        ]
        if not principals:
            # No RBP groups attached AND not all-employees. Default-deny: index
            # the doc but make it readable by nobody. Surfacing this at sync
            # time as a warning is recommended.
            principals = []

    return discoveryengine.Document(
        id=doc_id,
        struct_data=struct,
        content=discoveryengine.Document.Content(
            # PDF bytes — Discovery Engine handles text extraction.
            raw_bytes=policy.body_bytes,
            mime_type=policy.mime_type,
        ),
        acl_info=discoveryengine.Document.AclInfo(
            readers=[
                discoveryengine.Document.AclInfo.AccessRestriction(
                    principals=principals,
                )
            ],
        ),
    )
