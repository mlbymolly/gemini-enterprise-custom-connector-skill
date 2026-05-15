"""Rebuild the Identity Mapping Store from Workday + SuccessFactors RBP groups.

Inputs (you wire these to your real systems):
  - Workday RaaS report URL that returns rows like:
        { "workday_id": "...", "email": "jdoe@acme.com",
          "sap_user_id": "10042781", "rbp_groups": ["ALL_EMP", "MGR_NA"] }
  - Alternatively, pull RBP membership from SuccessFactors via the
    PermissionGroup OData entity and join it to Workday users on email or
    employee number.

The script emits two kinds of IdentityMappingEntry:
  - (externalIdentity = SAP user id, userId = Workspace email)
  - (externalIdentity = RBP group id, userId = each member's Workspace email)
    or, if you've mirrored RBP into Google Groups,
    (externalIdentity = RBP group id, groupId = group@acme.com).

Note: groups must be flattened to individual members (the IMS does not resolve
nested or implicit memberships server-side).
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Iterable

import requests
from google.cloud import discoveryengine_v1 as de

PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
IMS_ID = os.environ["IMS_ID"]

WORKDAY_RAAS_URL = os.environ["WORKDAY_RAAS_URL"]  # returns JSON array
WORKDAY_USER = os.environ["WORKDAY_USER"]
WORKDAY_PASS = os.environ["WORKDAY_PASS"]


def fetch_workday_roster() -> list[dict]:
    r = requests.get(
        WORKDAY_RAAS_URL,
        auth=(WORKDAY_USER, WORKDAY_PASS),
        params={"format": "json"},
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    # Workday RaaS wraps rows in Report_Entry.
    return body.get("Report_Entry", body)


def build_entries(roster: Iterable[dict]) -> list[de.IdentityMappingEntry]:
    entries: list[de.IdentityMappingEntry] = []

    # 1. User mappings: SAP user id -> Workspace email
    for row in roster:
        sap_uid = row.get("sap_user_id")
        email = row.get("email")
        if sap_uid and email:
            entries.append(de.IdentityMappingEntry(
                external_identity=str(sap_uid),
                user_id=email,
            ))

    # 2. Group expansion: RBP group id -> each member's email
    group_members: dict[str, set[str]] = defaultdict(set)
    for row in roster:
        email = row.get("email")
        if not email:
            continue
        for g in row.get("rbp_groups", []) or []:
            group_members[str(g)].add(email)

    for group_id, members in group_members.items():
        for email in members:
            entries.append(de.IdentityMappingEntry(
                external_identity=group_id,
                user_id=email,
            ))

    return entries


def import_entries(entries: list[de.IdentityMappingEntry]) -> None:
    client = de.IdentityMappingStoreServiceClient()
    store = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}"
        f"/identityMappingStores/{IMS_ID}"
    )
    op = client.import_identity_mappings(
        identity_mapping_store=store,
        inline_source=de.ImportIdentityMappingsRequest.InlineSource(
            identity_mapping_entries=entries,
        ),
    )
    print(f"Identity import op: {op.operation.name}")
    op.result()
    print(f"Imported {len(entries)} mappings")


def main() -> None:
    roster = fetch_workday_roster()
    entries = build_entries(roster)
    if not entries:
        print("No mappings to import.")
        return
    import_entries(entries)


if __name__ == "__main__":
    main()
