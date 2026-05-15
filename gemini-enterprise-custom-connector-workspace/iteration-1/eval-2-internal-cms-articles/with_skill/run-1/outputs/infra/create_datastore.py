"""One-time bootstrap: create the IMS (if missing) and the datastore.

Run this ONCE per environment before the first connector run. Setting
`acl_enabled=True` and binding the IMS can only happen at datastore creation
time -- if you forget either, you have to delete the datastore and re-import
everything.

Usage:
  PROJECT_ID=my-proj LOCATION=global \
  DATASTORE_ID=internal-cms-articles IMS_ID=internal-cms-ims \
  python infra/create_datastore.py
"""

from __future__ import annotations

import logging
import os

from google.api_core.exceptions import AlreadyExists
from google.cloud import discoveryengine_v1 as discoveryengine

# Re-use the IMS helper.
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from identity_mapping import bootstrap_ims, ims_resource_name  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("create-datastore")

PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
DATASTORE_ID = os.environ.get("DATASTORE_ID", "internal-cms-articles")


def create_datastore() -> None:
    # 1. Make sure the IMS exists. ACLs in transform.py use external_user/external_group
    #    prefixes, which require an IMS bound to the datastore.
    bootstrap_ims()

    # 2. Create the datastore with acl_enabled=True and the IMS reference.
    client = discoveryengine.DataStoreServiceClient()
    parent = client.collection_path(PROJECT_ID, LOCATION, "default_collection")
    try:
        op = client.create_data_store(
            request=discoveryengine.CreateDataStoreRequest(
                parent=parent,
                data_store_id=DATASTORE_ID,
                data_store=discoveryengine.DataStore(
                    display_name="Internal CMS Articles",
                    acl_enabled=True,  # MUST be set at creation; cannot be added later.
                    industry_vertical=discoveryengine.IndustryVertical.GENERIC,
                    identity_mapping_store=ims_resource_name(),
                    solution_types=[
                        discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH,
                    ],
                ),
            )
        )
        result = op.result()
        log.info("Created datastore: %s", result.name)
    except AlreadyExists:
        log.info("Datastore already exists: %s", DATASTORE_ID)


if __name__ == "__main__":
    create_datastore()
