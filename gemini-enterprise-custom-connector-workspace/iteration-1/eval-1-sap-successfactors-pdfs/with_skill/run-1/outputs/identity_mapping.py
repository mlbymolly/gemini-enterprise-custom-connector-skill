"""
Identity mapping for SAP SuccessFactors -> Gemini Enterprise.

Workday is the SSO source of truth in this deployment, so Workday holds the
authoritative mapping from SAP user id (e.g. "P12345") to Google Workspace
email. We pull that mapping on every connector run and push it into the
Identity Mapping Store. This keeps ACL resolution correct as employees join,
move, or leave.

Two kinds of mappings are loaded:

  1. USER mappings: SAP user id -> Google user (email)
     Used if document ACLs ever reference an individual SAP user.

  2. GROUP mappings: SuccessFactors RBP group id -> Google Group
     This is the common case. Documents are ACL'd to RBP groups
     ("EMEA_HR_MANAGERS", "GLOBAL_BENEFITS_ADMINS"), and a Workday integration
     job already maintains parallel Google Groups in Workspace
     (e.g. "emea-hr-managers@example.com"). The mapping just records that
     external RBP id <-> Workspace group.

Asymmetry to remember (from references/ingestion-connector.md):
  * When IMPORTING mappings: external_identity is BARE ("EMEA_HR_MANAGERS").
  * When REFERENCING the same identity in a document ACL: it's PREFIXED
    ("external_group:EMEA_HR_MANAGERS"). The IMS resolves the prefix at
    query time. transform.py already follows this convention.
"""

from __future__ import annotations

import logging
import os

from google.cloud import discoveryengine_v1 as discoveryengine

logger = logging.getLogger(__name__)


# -- Workday extract -----------------------------------------------------------

def fetch_workday_user_mappings() -> list[dict]:
    """Pull all active employees from Workday and return rows of
    {external: sap_user_id, user_id: google_email}.

    Implementation note: Workday exposes this through the Workday Web Services
    (Get_Workers) SOAP API or the REST 'Worker' resource. Both require an
    Integration System User (ISU) with workday-to-google-sso permissions. The
    expected query returns at minimum: workerId, primaryWorkEmail, and the
    custom field that stores the SAP/SuccessFactors userId.

    For brevity, the actual Workday call is stubbed. Plug in your existing
    Workday client here.
    """
    # client = workday.client(tenant=..., username=..., password=...)
    # workers = client.get_workers(active=True, include=["custom_sap_user_id"])
    # return [
    #     {"external": w.custom_sap_user_id, "user_id": w.primary_work_email}
    #     for w in workers if w.custom_sap_user_id and w.primary_work_email
    # ]
    raise NotImplementedError(
        "Plug in your Workday Get_Workers call. Return a list of "
        "{'external': '<sap_user_id>', 'user_id': '<google_email>'}."
    )


def fetch_workday_group_mappings() -> list[dict]:
    """Return rows of {external: rbp_group_id, group_id: google_group_email}.

    These come from the same Workday->Google Groups provisioning system that
    keeps Workspace groups in sync with SAP role-based-permission groups. If
    you don't yet have that provisioning loop, set it up first — without it,
    ACL enforcement on policies has nothing to resolve to.
    """
    raise NotImplementedError(
        "Plug in your Workday->Google Groups mapping source. Return a list of "
        "{'external': '<rbp_group_id>', 'group_id': '<group_email>'}."
    )


# -- Push mappings into the Identity Mapping Store -----------------------------

def _ims_name(project_id: str, location: str, ims_id: str) -> str:
    return f"projects/{project_id}/locations/{location}/identityMappingStores/{ims_id}"


def _import_mappings(ims: str, entries: list[dict]) -> None:
    """entries: [{external, user_id?, group_id?}]"""
    if not entries:
        return
    client = discoveryengine.IdentityMappingStoreServiceClient()

    # The inline import accepts up to 1000 entries per request — chunk if larger.
    CHUNK = 1000
    for i in range(0, len(entries), CHUNK):
        chunk = entries[i:i + CHUNK]
        inline_source = discoveryengine.ImportIdentityMappingsRequest.InlineSource(
            identity_mapping_entries=[
                discoveryengine.IdentityMappingEntry(
                    external_identity=e["external"],   # bare, no prefix
                    user_id=e.get("user_id"),
                    group_id=e.get("group_id"),
                )
                for e in chunk
            ]
        )
        op = client.import_identity_mappings(
            request=discoveryengine.ImportIdentityMappingsRequest(
                identity_mapping_store=ims,
                inline_source=inline_source,
            )
        )
        op.result()
        logger.info("Imported %d identity mappings into %s", len(chunk), ims)


def sync_identity_mappings_from_workday(project_id: str, location: str, ims_id: str) -> None:
    """Pull Workday user + group mappings and push them into the IMS."""
    ims = _ims_name(project_id, location, ims_id)

    user_rows = fetch_workday_user_mappings()
    group_rows = fetch_workday_group_mappings()

    logger.info("Workday returned %d user mappings, %d group mappings",
                len(user_rows), len(group_rows))

    _import_mappings(ims, user_rows)
    _import_mappings(ims, group_rows)
