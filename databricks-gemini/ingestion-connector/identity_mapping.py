"""Map Databricks identities (users, groups) to Google subjects in a
Discovery Engine Identity Mapping Store.

Asymmetry to remember:
- When *importing* a mapping, the external identity is bare ("data-engineers").
- When *referencing* it in a document ACL, prefix it ("external_group:data-engineers"
  or "external_user:alice").

The connector's transform.py already applies the `external_group:` prefix when
serializing ACLs, so the entries written here must be bare names.
"""

from dataclasses import dataclass
from typing import Iterable, Iterator

from google.cloud import discoveryengine_v1 as discoveryengine
from databricks.sdk import WorkspaceClient

# Inline import cap (matches the documented limit; chunk above this).
INLINE_MAX_ENTRIES = 100


@dataclass(frozen=True)
class Mapping:
    external_identity: str
    google_user: str | None = None
    google_group: str | None = None

    def to_entry(self) -> discoveryengine.IdentityMappingEntry:
        if bool(self.google_user) == bool(self.google_group):
            raise ValueError(
                f"mapping for {self.external_identity!r} must set exactly one "
                "of google_user or google_group"
            )
        return discoveryengine.IdentityMappingEntry(
            external_identity=self.external_identity,
            user_id=self.google_user,
            group_id=self.google_group,
        )


def ims_resource_name(project: str, location: str, ims_id: str) -> str:
    return f"projects/{project}/locations/{location}/identityMappingStores/{ims_id}"


def user_to_user(databricks_user_name: str, google_email: str | None = None) -> Mapping:
    """Map a Databricks user_name to a Google user email.

    Defaults to the Databricks user_name itself, which is the right thing when
    Databricks SSO is wired to Google Workspace (user_name == email).
    """
    return Mapping(
        external_identity=databricks_user_name,
        google_user=google_email or databricks_user_name,
    )


def group_to_google_group(databricks_group: str, google_group_email: str) -> Mapping:
    """Map a Databricks group to a single Google Workspace group (1:1)."""
    return Mapping(
        external_identity=databricks_group,
        google_group=google_group_email,
    )


def group_to_members(
    workspace: WorkspaceClient,
    databricks_group: str,
    user_overrides: dict[str, str] | None = None,
) -> Iterator[Mapping]:
    """Expand a Databricks group into one mapping per current member.

    Each entry has external_identity=<group_name>, user_id=<member email>.
    Discovery Engine collects them and resolves the group at query time.
    """
    user_overrides = user_overrides or {}
    group = _find_group_by_display_name(workspace, databricks_group)
    if group is None:
        raise ValueError(f"databricks group not found: {databricks_group!r}")

    for member in group.members or []:
        # `member` is a ComplexValue with `value` (id), `display`, `ref`.
        user_name = _resolve_user_name(workspace, member)
        if not user_name:
            continue
        email = user_overrides.get(user_name, user_name)
        yield Mapping(external_identity=databricks_group, google_user=email)


def _find_group_by_display_name(w: WorkspaceClient, name: str):
    for g in w.groups.list(filter=f'displayName eq "{name}"'):
        return g
    return None


def _resolve_user_name(w: WorkspaceClient, member) -> str | None:
    # SCIM member refs point at either a user or a nested group. We only
    # expand user members here — nested groups should have their own mapping.
    ref = (member.ref or "").lower()
    if "groups/" in ref:
        return None
    if member.display and "@" in member.display:
        return member.display
    if member.value:
        try:
            user = w.users.get(member.value)
            return user.user_name
        except Exception:
            return None
    return None


def import_mappings(
    client: discoveryengine.IdentityMappingStoreServiceClient,
    ims_name: str,
    mappings: Iterable[Mapping],
) -> int:
    """Push mappings to the IMS in chunks of INLINE_MAX_ENTRIES. Returns total imported."""
    buf: list[Mapping] = []
    total = 0

    def flush():
        nonlocal buf, total
        if not buf:
            return
        op = client.import_identity_mappings(
            request=discoveryengine.ImportIdentityMappingsRequest(
                identity_mapping_store=ims_name,
                inline_source=discoveryengine.ImportIdentityMappingsRequest.InlineSource(
                    identity_mapping_entries=[m.to_entry() for m in buf],
                ),
            )
        )
        op.result(timeout=600)
        total += len(buf)
        print(f"[ims] imported {total} mappings")
        buf = []

    for m in mappings:
        buf.append(m)
        if len(buf) >= INLINE_MAX_ENTRIES:
            flush()
    flush()
    return total


def purge_external_identity(
    client: discoveryengine.IdentityMappingStoreServiceClient,
    ims_name: str,
    external_identity: str,
) -> None:
    """Remove all mappings for a single external identity. Use before a fan-out
    re-import to drop stale members of a group."""
    op = client.purge_identity_mappings(
        request=discoveryengine.PurgeIdentityMappingsRequest(
            identity_mapping_store=ims_name,
            filter=f'external_identity = "{external_identity}"',
            force=True,
        )
    )
    op.result(timeout=600)
    print(f"[ims] purged mappings for {external_identity!r}")
