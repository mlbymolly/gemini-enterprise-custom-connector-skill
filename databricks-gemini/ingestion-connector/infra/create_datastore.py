"""One-time setup: create the Identity Mapping Store and the Discovery Engine
datastore. Run once per project; `acl_enabled=True` can only be set at creation.

    python infra/create_datastore.py \\
        --project my-gcp-project \\
        --location global \\
        --ims-id databricks-ims \\
        --datastore-id databricks-docs

Set --skip-ims if every reader is a Google Workspace email (no external identities).
"""

import argparse

from google.cloud import discoveryengine_v1 as discoveryengine


def create_ims(project: str, location: str, ims_id: str) -> str:
    client = discoveryengine.IdentityMappingStoreServiceClient()
    parent = f"projects/{project}/locations/{location}"
    op = client.create_identity_mapping_store(
        request=discoveryengine.CreateIdentityMappingStoreRequest(
            parent=parent,
            identity_mapping_store_id=ims_id,
            identity_mapping_store=discoveryengine.IdentityMappingStore(),
        )
    )
    name = op.name if hasattr(op, "name") else f"{parent}/identityMappingStores/{ims_id}"
    print(f"[ims] created: {name}")
    return f"{parent}/identityMappingStores/{ims_id}"


def create_datastore(
    project: str,
    location: str,
    datastore_id: str,
    ims_name: str | None,
) -> str:
    client = discoveryengine.DataStoreServiceClient()
    parent = client.collection_path(project, location, "default_collection")

    datastore = discoveryengine.DataStore(
        display_name="Databricks documents",
        acl_enabled=True,
        industry_vertical=discoveryengine.IndustryVertical.GENERIC,
        solution_types=[discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH],
    )
    if ims_name:
        datastore.identity_mapping_store = ims_name

    op = client.create_data_store(
        request=discoveryengine.CreateDataStoreRequest(
            parent=parent,
            data_store_id=datastore_id,
            data_store=datastore,
        )
    )
    result = op.result()
    print(f"[datastore] created: {result.name}")
    return result.name


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--location", default="global")
    p.add_argument("--ims-id", default="databricks-ims")
    p.add_argument("--datastore-id", required=True)
    p.add_argument("--skip-ims", action="store_true")
    args = p.parse_args()

    ims_name = None
    if not args.skip_ims:
        ims_name = create_ims(args.project, args.location, args.ims_id)

    create_datastore(args.project, args.location, args.datastore_id, ims_name)


if __name__ == "__main__":
    main()
