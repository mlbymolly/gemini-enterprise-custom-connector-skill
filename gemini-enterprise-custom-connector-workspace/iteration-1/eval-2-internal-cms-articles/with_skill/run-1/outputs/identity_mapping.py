"""Identity Mapping Store helpers.

The CMS uses internal usernames/groups that are NOT Google Workspace identities,
so we need an IMS to translate them to Google subjects at query time.

Run `bootstrap_ims()` ONCE per project before creating the datastore. Run
`sync_mappings(...)` whenever the CMS user/group roster changes (typically as
a separate scheduled job that reads from your IdP / HR system).
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from google.cloud import discoveryengine_v1 as discoveryengine

log = logging.getLogger(__name__)

PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
IMS_ID = os.environ.get("IMS_ID", "internal-cms-ims")


def ims_resource_name() -> str:
    return f"projects/{PROJECT_ID}/locations/{LOCATION}/identityMappingStores/{IMS_ID}"


def bootstrap_ims() -> str:
    """Create the Identity Mapping Store. Idempotent-ish: catches AlreadyExists."""
    from google.api_core.exceptions import AlreadyExists

    client = discoveryengine.IdentityMappingStoreServiceClient()
    parent = f"projects/{PROJECT_ID}/locations/{LOCATION}"
    try:
        op = client.create_identity_mapping_store(
            request=discoveryengine.CreateIdentityMappingStoreRequest(
                parent=parent,
                identity_mapping_store_id=IMS_ID,
                identity_mapping_store=discoveryengine.IdentityMappingStore(),
            )
        )
        result = op.result()
        log.info("Created IMS: %s", result.name)
        return result.name
    except AlreadyExists:
        log.info("IMS already exists: %s", ims_resource_name())
        return ims_resource_name()


def sync_mappings(entries: Iterable[dict]) -> None:
    """Import CMS-identity -> Google-subject mappings into the IMS.

    `entries` is an iterable of dicts like:
       {"external": "alice@corp.com", "user_id": "alice@google-workspace-domain.com"}
       {"external": "engineering",    "group_id": "eng-group@google-workspace-domain.com"}

    NOTE the asymmetry called out in references/ingestion-connector.md:
      - When IMPORTING here, the external identity is BARE: "alice@corp.com".
      - When REFERENCING the same identity in a Document ACL, you prefix it:
        "external_user:alice@corp.com" or "external_group:engineering".
    """
    client = discoveryengine.IdentityMappingStoreServiceClient()
    inline_source = discoveryengine.ImportIdentityMappingsRequest.InlineSource(
        identity_mapping_entries=[
            discoveryengine.IdentityMappingEntry(
                external_identity=e["external"],
                user_id=e.get("user_id"),
                group_id=e.get("group_id"),
            )
            for e in entries
        ]
    )
    op = client.import_identity_mappings(
        request=discoveryengine.ImportIdentityMappingsRequest(
            identity_mapping_store=ims_resource_name(),
            inline_source=inline_source,
        )
    )
    op.result()
    log.info("Identity mappings imported.")
