"""Pull Databricks identities and push the mappings into the IMS.

Usage:
    python infra/import_identities.py \\
        --project my-gcp-project \\
        --location global \\
        --ims-id databricks-ims \\
        --config mappings.json

Config schema (JSON):

    {
      "groups": [
        {"databricks_group": "data-engineers",
         "google_group": "data-eng@example.com"},
        {"databricks_group": "contractors",
         "expand_members": true},
        {"databricks_group": "vendors",
         "expand_members": true,
         "user_overrides": {"vendor_a.databricks": "vendor.a@example.com"}}
      ],
      "users": [
        {"databricks_user_name": "alice@example.com"}
      ],
      "user_overrides": {
        "service.account.databricks": "svc-account@example.com"
      }
    }

`google_group` and `expand_members` are mutually exclusive — pick one per group.
`user_overrides` at the top level applies to every expand_members entry that
doesn't set its own.

For groups with `expand_members: true`, the script optionally purges the
existing mappings for that external_identity first (`--repurge-expanded`), so
removed members fall out. Without the flag, removed members linger.
"""

import argparse
import json
import os
import sys

from google.cloud import discoveryengine_v1 as discoveryengine
from databricks.sdk import WorkspaceClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from identity_mapping import (  # noqa: E402
    Mapping,
    group_to_google_group,
    group_to_members,
    ims_resource_name,
    import_mappings,
    purge_external_identity,
    user_to_user,
)

DATABRICKS_HOST = os.environ["DATABRICKS_HOST"]
DATABRICKS_CLIENT_ID = os.environ["DATABRICKS_CLIENT_ID"]
DATABRICKS_CLIENT_SECRET = os.environ["DATABRICKS_CLIENT_SECRET"]


def _workspace() -> WorkspaceClient:
    return WorkspaceClient(
        host=f"https://{DATABRICKS_HOST}",
        client_id=DATABRICKS_CLIENT_ID,
        client_secret=DATABRICKS_CLIENT_SECRET,
    )


def _iter_mappings(config: dict, workspace: WorkspaceClient, repurge: bool, ims_client, ims_name):
    global_overrides = config.get("user_overrides") or {}

    for u in config.get("users") or []:
        yield user_to_user(
            databricks_user_name=u["databricks_user_name"],
            google_email=u.get("google_email"),
        )

    for g in config.get("groups") or []:
        name = g["databricks_group"]
        has_google_group = "google_group" in g
        expand = bool(g.get("expand_members"))
        if has_google_group == expand:
            raise ValueError(
                f"group {name!r}: set exactly one of google_group or expand_members"
            )

        if has_google_group:
            yield group_to_google_group(name, g["google_group"])
            continue

        # expand_members path
        if repurge:
            purge_external_identity(ims_client, ims_name, name)
        overrides = {**global_overrides, **(g.get("user_overrides") or {})}
        count = 0
        for m in group_to_members(workspace, name, user_overrides=overrides):
            count += 1
            yield m
        print(f"[expand] {name}: {count} member(s)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--location", default="global")
    p.add_argument("--ims-id", required=True)
    p.add_argument("--config", required=True, help="Path to mappings.json")
    p.add_argument(
        "--repurge-expanded",
        action="store_true",
        help="Purge existing mappings for each expand_members group before re-importing. "
        "Use this to evict members who left the group.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print mappings without calling the IMS.",
    )
    args = p.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    ims_client = discoveryengine.IdentityMappingStoreServiceClient()
    ims_name = ims_resource_name(args.project, args.location, args.ims_id)
    workspace = _workspace()

    mappings = list(_iter_mappings(config, workspace, args.repurge_expanded, ims_client, ims_name))
    print(f"[plan] {len(mappings)} mapping(s) to import")

    if args.dry_run:
        for m in mappings[:20]:
            print(f"  {m}")
        if len(mappings) > 20:
            print(f"  ... and {len(mappings) - 20} more")
        return

    if not mappings:
        print("[ims] nothing to import")
        return

    total = import_mappings(ims_client, ims_name, mappings)
    print(f"[done] imported {total} mapping(s) into {ims_name}")


if __name__ == "__main__":
    main()
