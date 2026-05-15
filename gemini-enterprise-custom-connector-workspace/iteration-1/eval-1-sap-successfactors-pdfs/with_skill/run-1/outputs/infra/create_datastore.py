"""
One-time setup script.

Run this BEFORE the first connector job:
  1. Create the Identity Mapping Store (IMS).
  2. Create the Discovery Engine datastore with acl_enabled=True and the IMS
     attached.

CRITICAL: acl_enabled=True and identity_mapping_store both can ONLY be set at
datastore creation. If you forget either, you have to delete the datastore and
re-create it (and re-import all documents).

Usage:
  export PROJECT_ID=my-gcp-project
  export LOCATION=global
  export IMS_ID=sap-successfactors-ims
  export DATASTORE_ID=sap-hr-policies
  python infra/create_datastore.py
"""

from __future__ import annotations

import os
import sys

from google.cloud import discoveryengine_v1 as discoveryengine
from google.api_core.exceptions import AlreadyExists

PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
IMS_ID = os.environ["IMS_ID"]
DATASTORE_ID = os.environ["DATASTORE_ID"]


def create_ims() -> str:
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
        ims = op.result()
        print(f"Created IMS: {ims.name}")
    except AlreadyExists:
        print(f"IMS already exists: {IMS_ID}")
    return f"projects/{PROJECT_ID}/locations/{LOCATION}/identityMappingStores/{IMS_ID}"


def create_datastore(ims_name: str) -> None:
    client = discoveryengine.DataStoreServiceClient()
    parent = client.collection_path(PROJECT_ID, LOCATION, "default_collection")
    try:
        op = client.create_data_store(
            request=discoveryengine.CreateDataStoreRequest(
                parent=parent,
                data_store_id=DATASTORE_ID,
                data_store=discoveryengine.DataStore(
                    display_name="SAP SuccessFactors HR Policies",
                    acl_enabled=True,                              # immutable
                    industry_vertical=discoveryengine.IndustryVertical.GENERIC,
                    identity_mapping_store=ims_name,               # immutable
                    solution_types=[
                        discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH,
                    ],
                    content_config=discoveryengine.DataStore.ContentConfig.CONTENT_REQUIRED,
                ),
            )
        )
        ds = op.result()
        print(f"Created datastore: {ds.name}")
    except AlreadyExists:
        print(f"Datastore already exists: {DATASTORE_ID}")
        print("If acl_enabled or identity_mapping_store is wrong on the "
              "existing datastore, DELETE and recreate — they can't be patched.")
        sys.exit(1)


if __name__ == "__main__":
    ims_name = create_ims()
    create_datastore(ims_name)
    print("Setup complete.")
    print(f"Next: deploy connector.py as a Cloud Run Job with DATASTORE_ID="
          f"{DATASTORE_ID} and IMS_ID={IMS_ID}.")
