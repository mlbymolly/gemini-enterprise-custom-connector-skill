"""One-time setup: create the Identity Mapping Store and the data store bound to it."""
import os
from google.cloud import discoveryengine_v1 as de

PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
DATA_STORE_ID = os.environ["DATA_STORE_ID"]
IMS_ID = os.environ["IMS_ID"]


def get_or_create_ims() -> str:
    client = de.IdentityMappingStoreServiceClient()
    name = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}/identityMappingStores/{IMS_ID}"
    )
    try:
        client.get_identity_mapping_store(name=name)
        print(f"IMS already exists: {name}")
        return name
    except Exception:
        parent = f"projects/{PROJECT_ID}/locations/{LOCATION}"
        client.create_identity_mapping_store(
            parent=parent,
            identity_mapping_store=de.IdentityMappingStore(),
            identity_mapping_store_id=IMS_ID,
        )
        print(f"Created IMS: {name}")
        return name


def get_or_create_data_store(ims_name: str) -> str:
    client = de.DataStoreServiceClient()
    ds_name = client.data_store_path(PROJECT_ID, LOCATION, DATA_STORE_ID)
    try:
        client.get_data_store(name=ds_name)
        print(f"Data store already exists: {ds_name}")
        return ds_name
    except Exception:
        parent = client.collection_path(PROJECT_ID, LOCATION, "default_collection")
        op = client.create_data_store(
            parent=parent,
            data_store_id=DATA_STORE_ID,
            data_store=de.DataStore(
                display_name="SAP SuccessFactors - HR Policies",
                acl_enabled=True,
                industry_vertical=de.IndustryVertical.GENERIC,
                solution_types=[de.SolutionType.SOLUTION_TYPE_SEARCH],
                content_config=de.DataStore.ContentConfig.CONTENT_REQUIRED,
                identity_mapping_store=ims_name,
            ),
        )
        op.result()
        print(f"Created data store: {ds_name}")
        return ds_name


if __name__ == "__main__":
    ims = get_or_create_ims()
    get_or_create_data_store(ims)
