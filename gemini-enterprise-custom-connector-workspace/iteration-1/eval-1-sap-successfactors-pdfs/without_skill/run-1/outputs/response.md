# Building a Gemini Enterprise Custom Connector for SAP SuccessFactors HR Policy PDFs

This guide gives you a complete, runnable design for a custom connector that pulls HR policy PDFs from SAP SuccessFactors and lands them in a Gemini Enterprise data store so your agent can cite them. It covers the tricky bit — your SAP user IDs don't match Google emails because Workday is the system of record for SSO — using a Gemini Enterprise **Identity Mapping Store**.

## Assumptions (state and adjust as needed)

1. **PDFs live in SuccessFactors as Attachments.** I'm assuming policy PDFs are stored either against the `Attachment` OData entity in Employee Central, or as MDF-object attachments (e.g., a custom MDF like `HRPolicyDocument`). The fetcher handles both with minor tweaks. If your tenant uses Document Management Service (DMS) — i.e., the Manage Documents tool — the same pattern applies but you swap in the DMS APIs.
2. **Auth to SuccessFactors uses OAuth 2.0 SAML Bearer Assertion** (Basic auth is deprecated). You have a registered OAuth client, an X.509 keypair, and an `apiUser` with permission to read attachments / MDF objects.
3. **Workday is the system of record for identity.** Every employee has a Workday-issued Google Workspace email (e.g., `jdoe@acme.com`). Their SuccessFactors user ID (often `personIdExternal` or `userId`) is a different value (e.g., `10042781`). We will keep a Workday-driven mapping (Workday Report-as-a-Service / RaaS, or your Workday → IdP feed) and load it into the Gemini Enterprise Identity Mapping Store keyed by the SAP user ID.
4. **Permissions are role-based, not per-document.** HR policies in SuccessFactors are typically gated by RBP (Role-Based Permission) groups, e.g., "All Employees", "Managers - NA", "HR Admins". We'll model these as **external groups** in the identity mapping store, and put `external_group:<rbp-group-id>` in each document's `aclInfo.readers`. If your policies are actually world-readable inside the company, you can replace ACLs with `{"idp_wide": true}`.
5. **Google Cloud project** with Discovery Engine API enabled, a GCS bucket (`gs://acme-ge-successfactors-pdfs`) for staged PDFs and JSONL, and a service account with `roles/discoveryengine.admin` + `roles/storage.objectAdmin`.
6. **Workforce Identity Federation (or Cloud Identity) is wired up** so Gemini Enterprise can see your authenticated users by their `@acme.com` email. This is the "Google side" of the mapping.
7. **You want incremental syncs.** SuccessFactors attachments expose `lastModifiedDateTime` — we filter on it with an OData `$filter` and store a high-water mark in GCS between runs.

---

## Architecture at a glance

```
                +--------------------------+
                |  SAP SuccessFactors      |
                |  - Attachment OData v2   |
                |  - MDF objects (policies)|
                |  - RBP groups            |
                +-----+--------------+-----+
                      |              |
   OAuth2 SAML Bearer | (PDFs +      | (group membership +
                      |  metadata)   |  policy->group ACLs)
                      v              v
              +-------+--------------+-------+
              |   Connector (Cloud Run Job)  |
              |   - fetch policies + ACLs    |
              |   - decode base64 PDF        |
              |   - upload PDF -> GCS        |
              |   - emit metadata.jsonl      |
              |   - emit identity mappings   |
              +------+----------------+------+
                     |                |
                     v                v
        gs://.../pdfs/*.pdf     identity_mappings.jsonl
        gs://.../metadata.jsonl       |
                     |                |
                     v                v
+--------------------+----+   +-------+------------------+
| Discovery Engine        |   | Identity Mapping Store   |
| Data Store (acl_enabled)|<--+ (sap_user_id -> email)   |
|  - PDFs (content.uri)   |   | (rbp_group -> WD group)  |
|  - aclInfo.readers      |   +--------------------------+
+----------+--------------+
           |
           v
   Gemini Enterprise App  ->  Agent cites HR policies (ACL-respecting)
```

**The identity story in one sentence:** when `jdoe@acme.com` asks the agent a question, Gemini Enterprise looks at the document's `aclInfo` (which lists SAP/RBP IDs), resolves those IDs to Google identities via the bound Identity Mapping Store, and only returns the policies Jane is actually allowed to see.

---

## Why an Identity Mapping Store (and not just rewriting IDs)

You could try to "translate" SAP user IDs to emails at ingest time, but that breaks two things:

- **Groups don't translate cleanly.** RBP groups like `HR_ADMIN_GLOBAL` aren't Google groups; embedding member emails directly into every doc explodes the ACL and goes stale instantly.
- **Re-permissioning requires re-ingesting documents.** With an Identity Mapping Store, when someone joins a group in Workday/SAP you re-load the mapping (cheap) — you don't have to touch the docs.

The Identity Mapping Store is exactly the right primitive here: it lets you put **SAP-native identifiers** in `aclInfo` (as `externalEntityId`) and have Gemini Enterprise resolve them to the searching user's Google identity at query time.

---

## Step-by-step build

### 1. One-time GCP setup

```bash
export PROJECT_ID=acme-ge-prod
export LOCATION=global
export DATA_STORE_ID=successfactors-hr-policies
export IMS_ID=successfactors-identities
export BUCKET=acme-ge-successfactors-pdfs

gcloud config set project "$PROJECT_ID"
gcloud services enable discoveryengine.googleapis.com storage.googleapis.com
gcloud storage buckets create "gs://$BUCKET" --location=us
```

Create a service account for the connector:

```bash
gcloud iam service-accounts create ge-sf-connector \
  --display-name="Gemini Enterprise SuccessFactors Connector"

for ROLE in roles/discoveryengine.admin roles/storage.objectAdmin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:ge-sf-connector@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="$ROLE"
done
```

### 2. Create the Identity Mapping Store and bind it to a data store

You must do this **before** creating the data store — the binding is set at creation time and is immutable.

See `setup_data_store.py` (included below). It:
1. Creates the Identity Mapping Store `successfactors-identities`.
2. Creates a data store `successfactors-hr-policies` with `acl_enabled=True` and `identity_mapping_store=projects/.../identityMappingStores/successfactors-identities`.

### 3. Wire up SuccessFactors auth (OAuth2 SAML Bearer)

The high-level flow:
1. In SuccessFactors **Admin Center → Manage OAuth2 Client Applications**, register a client with your X.509 cert. Note the API key.
2. Create an API-only `apiUser` with the **Manage Documents / Read Attachments** permissions and the relevant RBP permissions for the policies you want to surface.
3. At runtime, the connector generates a signed SAML assertion (`subject = apiUser`, `audience = www.successfactors.com`, signed by the cert's private key) and exchanges it for an access token at `https://api{N}.successfactors.com/oauth/token`.
4. Use the bearer token for OData calls.

See `successfactors_client.py` for the implementation.

### 4. Fetch the policies and their ACLs

For each HR policy PDF:
- `GET /odata/v2/Attachment(<id>)?$select=attachmentId,fileName,mimeType,fileContent,lastModifiedDateTime,module,userId` returns the file content as a **base64-encoded gzipped** string. Decode + ungzip.
- For metadata (title, effective date, locale, owner group), pull the parent MDF object (or whatever holds your policy record), e.g., `cust_HRPolicy`.
- For the ACL, read the RBP groups associated with that policy. In most tenants this is captured on the MDF object itself via a `cust_visibleToGroups` field, or via an `AccessGroupMembership` style join. Adjust the `_resolve_acl_for_policy` function for your tenant.

### 5. Stage PDFs + metadata to GCS, then import

Two artifacts go to GCS:
- The PDFs themselves at `gs://$BUCKET/pdfs/<doc-id>.pdf`.
- A `metadata.jsonl` file describing each document, pointing at the PDF via `content.uri` and including `aclInfo`.

Then call `ImportDocuments` with `reconciliation_mode=FULL` so deletions in SuccessFactors propagate.

### 6. Build the identity mapping from Workday

The mapping connects SAP-native identifiers to Google identities. You'll usually generate this from a Workday RaaS report (e.g., `INT_GoogleWorkspace_Roster`) joined to SuccessFactors RBP group membership. Two kinds of entries:

```json
{"externalIdentity": "10042781", "userId": "jdoe@acme.com"}
{"externalIdentity": "HR_ADMIN_GLOBAL", "groupId": "hr-admins@acme.com"}
```

- The **user mapping** ties a SAP `userId` (or `personIdExternal`) to the Workday-issued Google email.
- The **group mapping** ties an RBP group ID to either a Google group email (if you've mirrored RBP into Google Groups) or to a flattened list of user mappings.

In documents we reference these as `externalEntityId: "external_group:HR_ADMIN_GLOBAL"` (note the `external_group:` prefix for groups in the doc ACL — but **not** in the mapping store entries).

Push via `ImportIdentityMappings` (full reconciliation) on the same cadence as the doc sync.

### 7. Schedule

Run the connector as a Cloud Run Job on a Cloud Scheduler cron (e.g., every 30 min). The job is idempotent and uses `reconciliation_mode=FULL`, so a missed run is harmless.

---

## Code

All files referenced below are saved alongside this response in the `outputs/` directory.

- `setup_data_store.py` — one-time setup of Identity Mapping Store + data store
- `successfactors_client.py` — OAuth2 SAML bearer auth + attachment fetcher
- `connector.py` — main sync entrypoint: fetch -> transform -> stage -> import
- `identity_sync.py` — pulls Workday + SF group membership and rebuilds the IMS
- `requirements.txt`
- `Dockerfile`
- `cloud_run_job.yaml` — example Cloud Run Job manifest

### `requirements.txt`

```
google-cloud-discoveryengine>=0.13.0
google-cloud-storage>=2.18.0
requests>=2.32.0
PyJWT>=2.9.0
cryptography>=43.0.0
signxml>=4.0.0
lxml>=5.3.0
```

### `setup_data_store.py`

```python
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
```

### `successfactors_client.py`

```python
"""SAP SuccessFactors OAuth2 SAML Bearer Assertion client + Attachment fetcher.

Handles:
- Building & signing a SAML assertion with the registered X.509 cert.
- Exchanging it for an OAuth2 bearer token.
- Paginated OData reads with $filter on lastModifiedDateTime for incremental sync.
- Decoding the base64+gzip attachment payload back to raw PDF bytes.
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import time
import uuid
from dataclasses import dataclass
from typing import Iterator

import requests
from lxml import etree
from signxml import XMLSigner, methods


SAML_NS = {
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
}


@dataclass
class SFConfig:
    api_host: str           # e.g. "api4.successfactors.com"
    company_id: str         # e.g. "ACME"
    client_id: str          # OAuth2 API key registered in SF
    api_user: str           # apiUser username (the "subject" of the SAML)
    token_url: str          # e.g. f"https://{api_host}/oauth/token"
    private_key_pem: bytes  # PEM-encoded private key matching the cert in SF
    certificate_pem: bytes  # PEM-encoded X.509 cert registered in SF


def _build_saml_assertion(cfg: SFConfig) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    not_before = now - dt.timedelta(minutes=5)
    not_on_or_after = now + dt.timedelta(minutes=10)
    assertion_id = f"_{uuid.uuid4().hex}"

    nsmap = {"saml": SAML_NS["saml"]}
    root = etree.Element(
        "{%s}Assertion" % SAML_NS["saml"],
        nsmap=nsmap,
        attrib={
            "ID": assertion_id,
            "IssueInstant": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": "2.0",
        },
    )
    etree.SubElement(root, "{%s}Issuer" % SAML_NS["saml"]).text = cfg.client_id

    subj = etree.SubElement(root, "{%s}Subject" % SAML_NS["saml"])
    nid = etree.SubElement(
        subj, "{%s}NameID" % SAML_NS["saml"],
        Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
    )
    nid.text = cfg.api_user
    sconf = etree.SubElement(
        subj, "{%s}SubjectConfirmation" % SAML_NS["saml"],
        Method="urn:oasis:names:tc:SAML:2.0:cm:bearer",
    )
    etree.SubElement(
        sconf, "{%s}SubjectConfirmationData" % SAML_NS["saml"],
        NotOnOrAfter=not_on_or_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        Recipient=cfg.token_url,
    )

    cond = etree.SubElement(
        root, "{%s}Conditions" % SAML_NS["saml"],
        NotBefore=not_before.strftime("%Y-%m-%dT%H:%M:%SZ"),
        NotOnOrAfter=not_on_or_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    audr = etree.SubElement(cond, "{%s}AudienceRestriction" % SAML_NS["saml"])
    etree.SubElement(audr, "{%s}Audience" % SAML_NS["saml"]).text = "www.successfactors.com"

    astmt = etree.SubElement(root, "{%s}AuthnStatement" % SAML_NS["saml"],
                             AuthnInstant=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    actx = etree.SubElement(astmt, "{%s}AuthnContext" % SAML_NS["saml"])
    etree.SubElement(actx, "{%s}AuthnContextClassRef" % SAML_NS["saml"]).text = (
        "urn:oasis:names:tc:SAML:2.0:ac:classes:PreviousSession"
    )

    # Add API URL attribute (SF expects this)
    attr_stmt = etree.SubElement(root, "{%s}AttributeStatement" % SAML_NS["saml"])
    attr = etree.SubElement(
        attr_stmt, "{%s}Attribute" % SAML_NS["saml"], Name="api_url"
    )
    etree.SubElement(attr, "{%s}AttributeValue" % SAML_NS["saml"]).text = (
        f"https://{cfg.api_host}"
    )

    signed = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    ).sign(root, key=cfg.private_key_pem, cert=cfg.certificate_pem)

    xml_bytes = etree.tostring(signed)
    return base64.b64encode(xml_bytes).decode("ascii")


class SuccessFactorsClient:
    def __init__(self, cfg: SFConfig):
        self.cfg = cfg
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._session = requests.Session()

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        assertion = _build_saml_assertion(self.cfg)
        resp = self._session.post(
            self.cfg.token_url,
            data={
                "client_id": self.cfg.client_id,
                "company_id": self.cfg.company_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:saml2-bearer",
                "assertion": assertion,
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        # SF tokens are usually short-lived (~12h). Trust the response.
        self._token_expires_at = time.time() + int(body.get("expires_in", 43200))
        return self._token

    def _odata_get(self, path: str, params: dict | None = None) -> dict:
        url = f"https://{self.cfg.api_host}/odata/v2/{path}"
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
        }
        params = {"$format": "json", **(params or {})}
        r = self._session.get(url, headers=headers, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def iter_entity(
        self,
        entity: str,
        select: list[str],
        filter_expr: str | None = None,
        page_size: int = 200,
    ) -> Iterator[dict]:
        """Yield records from an OData entity using server-driven paging."""
        params = {"$select": ",".join(select), "$top": page_size, "$skip": 0}
        if filter_expr:
            params["$filter"] = filter_expr
        while True:
            data = self._odata_get(entity, params)
            results = data.get("d", {}).get("results", [])
            for row in results:
                yield row
            if len(results) < page_size:
                return
            params["$skip"] += page_size

    @staticmethod
    def decode_attachment(file_content_b64: str) -> bytes:
        """SF returns attachment fileContent as base64-encoded gzipped bytes.

        Some tenants/configs return plain base64 (no gzip). We try gzip first
        and fall back to the raw decoded bytes.
        """
        raw = base64.b64decode(file_content_b64)
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw

    def fetch_attachment_bytes(self, attachment_id: str) -> tuple[bytes, dict]:
        """Returns (pdf_bytes, attachment_metadata)."""
        data = self._odata_get(
            f"Attachment({attachment_id})",
            {"$select": "attachmentId,fileName,mimeType,fileContent,"
                        "lastModifiedDateTime,module,userId"},
        )
        record = data["d"]
        pdf_bytes = self.decode_attachment(record.pop("fileContent"))
        return pdf_bytes, record
```

### `connector.py`

```python
"""Sync HR policy PDFs from SAP SuccessFactors to a Gemini Enterprise data store.

Steps per run:
  1. Read the last high-water mark from gs://$BUCKET/state/last_sync.txt.
  2. Query SuccessFactors for HR policies modified since then (MDF object cust_HRPolicy).
  3. For each policy, download its attachment, resolve its RBP group ACL.
  4. Upload PDF -> gs://$BUCKET/pdfs/<policyId>.pdf.
  5. Emit metadata.jsonl describing every policy.
  6. ImportDocuments(reconciliation_mode=FULL) into the data store.
  7. Write a new high-water mark.

The high-water mark is for observability/logging; reconciliation_mode=FULL means
the import is a complete replacement based on the current run's contents.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
from typing import Iterable

from google.cloud import discoveryengine_v1 as de
from google.cloud import storage

from successfactors_client import SFConfig, SuccessFactorsClient

PROJECT_ID = os.environ["PROJECT_ID"]
LOCATION = os.environ.get("LOCATION", "global")
DATA_STORE_ID = os.environ["DATA_STORE_ID"]
BUCKET = os.environ["BUCKET"]
SF_API_HOST = os.environ["SF_API_HOST"]
SF_COMPANY_ID = os.environ["SF_COMPANY_ID"]
SF_CLIENT_ID = os.environ["SF_CLIENT_ID"]
SF_API_USER = os.environ["SF_API_USER"]
SF_PRIVATE_KEY = os.environ["SF_PRIVATE_KEY_PEM"].encode("utf-8")
SF_CERT = os.environ["SF_CERT_PEM"].encode("utf-8")

# Adjust the MDF entity + ACL field names for your tenant.
POLICY_ENTITY = os.environ.get("SF_POLICY_ENTITY", "cust_HRPolicy")
POLICY_SELECT = [
    "externalCode",          # we use this as the stable doc ID
    "cust_title",
    "cust_summary",
    "cust_effectiveDate",
    "cust_locale",
    "cust_owner",
    "cust_attachmentId_attachment_attachmentId",  # joins to Attachment
    "cust_visibleToGroups",  # comma-separated list of RBP group IDs
    "lastModifiedDateTime",
]


def _gcs_blob(client: storage.Client, path: str) -> storage.Blob:
    return client.bucket(BUCKET).blob(path)


def _read_high_water_mark(client: storage.Client) -> str | None:
    blob = _gcs_blob(client, "state/last_sync.txt")
    if not blob.exists():
        return None
    return blob.download_as_text().strip()


def _write_high_water_mark(client: storage.Client, ts: str) -> None:
    _gcs_blob(client, "state/last_sync.txt").upload_from_string(ts)


def _odata_filter_since(ts: str | None) -> str | None:
    if not ts:
        return None
    # SF wants the literal datetime in OData format.
    return f"lastModifiedDateTime ge datetimeoffset'{ts}'"


def _resolve_acl(visible_to_groups: str | None) -> de.Document.AclInfo:
    """Convert a comma-separated list of RBP group IDs into Discovery Engine ACL info.

    If a policy is intended to be visible to everyone in the organization, you can
    return an idp_wide reader instead.
    """
    if not visible_to_groups:
        # Default to "no one" if nothing is set; safer than implicit open.
        return de.Document.AclInfo(readers=[de.Document.AclInfo.AccessRestriction(
            principals=[]
        )])
    principals = []
    for group_id in (g.strip() for g in visible_to_groups.split(",") if g.strip()):
        principals.append(de.Principal(
            external_entity_id=f"external_group:{group_id}"
        ))
    return de.Document.AclInfo(
        readers=[de.Document.AclInfo.AccessRestriction(principals=principals)]
    )


def _build_documents(
    sf: SuccessFactorsClient,
    storage_client: storage.Client,
    since: str | None,
) -> list[de.Document]:
    docs: list[de.Document] = []
    bucket = storage_client.bucket(BUCKET)
    for policy in sf.iter_entity(
        POLICY_ENTITY,
        select=POLICY_SELECT,
        filter_expr=_odata_filter_since(since),
    ):
        doc_id = policy["externalCode"]
        attachment_id = policy.get("cust_attachmentId_attachment_attachmentId")
        if not attachment_id:
            continue
        pdf_bytes, att_meta = sf.fetch_attachment_bytes(str(attachment_id))

        # Stage PDF in GCS.
        pdf_blob = bucket.blob(f"pdfs/{doc_id}.pdf")
        pdf_blob.upload_from_file(
            io.BytesIO(pdf_bytes), content_type="application/pdf"
        )
        gcs_uri = f"gs://{BUCKET}/pdfs/{doc_id}.pdf"

        struct_data = {
            "title": policy.get("cust_title") or att_meta.get("fileName"),
            "summary": policy.get("cust_summary"),
            "effective_date": policy.get("cust_effectiveDate"),
            "locale": policy.get("cust_locale"),
            "owner_sap_user_id": policy.get("cust_owner"),
            "source_system": "sap_successfactors",
            "source_url": (
                f"https://{SF_API_HOST.replace('api', 'performancemanager')}"
                f"/sf/successfactors?company={SF_COMPANY_ID}#policy/{doc_id}"
            ),
            "last_modified": policy.get("lastModifiedDateTime"),
        }

        doc = de.Document(
            id=doc_id,
            content=de.Document.Content(
                mime_type="application/pdf",
                uri=gcs_uri,
            ),
            struct_data=struct_data,  # promoted into metadata for filtering / citations
            acl_info=_resolve_acl(policy.get("cust_visibleToGroups")),
        )
        docs.append(doc)
    return docs


def _stage_metadata_jsonl(
    storage_client: storage.Client,
    docs: list[de.Document],
) -> str:
    lines: list[str] = []
    for d in docs:
        lines.append(de.Document.to_json(d, indent=None))
    blob_path = f"staging/metadata-{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}.jsonl"
    storage_client.bucket(BUCKET).blob(blob_path).upload_from_string(
        "\n".join(lines) + "\n", content_type="application/json"
    )
    return f"gs://{BUCKET}/{blob_path}"


def _import_documents(gcs_uri: str) -> None:
    client = de.DocumentServiceClient()
    parent = client.branch_path(
        project=PROJECT_ID,
        location=LOCATION,
        data_store=DATA_STORE_ID,
        branch="default_branch",
    )
    op = client.import_documents(
        parent=parent,
        gcs_source=de.GcsSource(
            input_uris=[gcs_uri], data_schema="document"
        ),
        reconciliation_mode=de.ImportDocumentsRequest.ReconciliationMode.FULL,
    )
    print(f"Import op: {op.operation.name}")
    op.result()  # blocks until done


def main() -> int:
    sf = SuccessFactorsClient(SFConfig(
        api_host=SF_API_HOST,
        company_id=SF_COMPANY_ID,
        client_id=SF_CLIENT_ID,
        api_user=SF_API_USER,
        token_url=f"https://{SF_API_HOST}/oauth/token",
        private_key_pem=SF_PRIVATE_KEY,
        certificate_pem=SF_CERT,
    ))
    storage_client = storage.Client()

    since = _read_high_water_mark(storage_client)
    print(f"Last successful sync: {since or '(initial run)'}")

    docs = _build_documents(sf, storage_client, since=None)
    # Note: we always pull the full set for FULL reconciliation. The high-water
    # mark is purely for logging/observability. If your tenant is large, switch
    # to INCREMENTAL and pass `since` into _build_documents.
    if not docs:
        print("No documents found.")
        return 0

    gcs_uri = _stage_metadata_jsonl(storage_client, docs)
    print(f"Staged {len(docs)} docs at {gcs_uri}")
    _import_documents(gcs_uri)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_high_water_mark(storage_client, now)
    print(f"Done. High-water mark set to {now}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### `identity_sync.py`

```python
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
```

### `Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "connector.py"]
```

### `cloud_run_job.yaml`

```yaml
# Deploy with:
#   gcloud run jobs replace cloud_run_job.yaml --region=us-central1
#   gcloud scheduler jobs create http sf-policy-sync \
#     --schedule="*/30 * * * *" \
#     --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/sf-policy-sync:run" \
#     --http-method=POST \
#     --oauth-service-account-email=ge-sf-connector@$PROJECT_ID.iam.gserviceaccount.com
apiVersion: run.googleapis.com/v1
kind: Job
metadata:
  name: sf-policy-sync
spec:
  template:
    spec:
      template:
        spec:
          serviceAccountName: ge-sf-connector@acme-ge-prod.iam.gserviceaccount.com
          timeoutSeconds: 3600
          containers:
            - image: us-central1-docker.pkg.dev/acme-ge-prod/connectors/sf-policy-sync:latest
              env:
                - {name: PROJECT_ID,    value: acme-ge-prod}
                - {name: LOCATION,      value: global}
                - {name: DATA_STORE_ID, value: successfactors-hr-policies}
                - {name: IMS_ID,        value: successfactors-identities}
                - {name: BUCKET,        value: acme-ge-successfactors-pdfs}
                - {name: SF_API_HOST,   value: api4.successfactors.com}
                - {name: SF_COMPANY_ID, value: ACME}
                - {name: SF_CLIENT_ID,  valueFrom: {secretKeyRef: {name: sf-client-id,  key: latest}}}
                - {name: SF_API_USER,   valueFrom: {secretKeyRef: {name: sf-api-user,   key: latest}}}
                - {name: SF_PRIVATE_KEY_PEM, valueFrom: {secretKeyRef: {name: sf-private-key, key: latest}}}
                - {name: SF_CERT_PEM,        valueFrom: {secretKeyRef: {name: sf-cert,        key: latest}}}
```

---

## Quick-start runbook

```bash
# 0. One-time
python setup_data_store.py

# 1. Load the identity mapping (do this before, or at least alongside, the first doc sync)
python identity_sync.py

# 2. Initial doc sync
python connector.py

# 3. Verify ACLs end-to-end (returns only docs the caller can read)
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  "https://discoveryengine.googleapis.com/v1/projects/$PROJECT_ID/locations/global/collections/default_collection/dataStores/$DATA_STORE_ID/servingConfigs/default_search:search" \
  -d '{"query":"parental leave policy","pageSize":5}'

# 4. Wire the data store into a Gemini Enterprise app
#    (Console: Gemini Enterprise -> Apps -> Add data store -> select successfactors-hr-policies)
```

---

## Verification checklist

- [ ] `gcloud discoveryengine identity-mapping-stores list` shows `successfactors-identities`.
- [ ] `gcloud discoveryengine data-stores describe successfactors-hr-policies` shows `aclEnabled: true` and `identityMappingStore` pointing at the IMS.
- [ ] One sample PDF appears at `gs://$BUCKET/pdfs/...`.
- [ ] `metadata.jsonl` contains `content.uri`, `content.mimeType=application/pdf`, and a non-empty `aclInfo.readers[0].principals`.
- [ ] A test user who is in the RBP group can retrieve the doc; a user who is not, cannot.
- [ ] The agent's answer includes a citation back to the policy with `source_url` pointing at the SuccessFactors UI.

---

## Common pitfalls

- **Forgetting the `external_group:` prefix.** It goes in the **document ACL** principal (`externalEntityId: "external_group:HR_ADMIN_GLOBAL"`), but **not** in the identity-mapping-store entry (`externalIdentity: "HR_ADMIN_GLOBAL"`). Mismatch and ACLs silently match nothing.
- **Setting the IMS after data store creation.** You can't. Delete and recreate the data store if you skipped it on day 1.
- **Email-format requirement.** `userId` in IMS entries (and in `aclInfo.principals`) must be in email format — use the Workspace email, not a Workday GUID.
- **Nested RBP groups.** The IMS doesn't recurse — flatten members before import.
- **Non-native PDFs.** If your HR PDFs are scanned/image-based, enable the layout parser (or OCR) when creating the data store so citations include readable text spans.
- **Attachment payload encoding.** Some SF tenants gzip the base64; others don't. The decoder in `successfactors_client.py` tries gzip first and falls back to raw — keep that pattern.
- **Reconciliation mode.** Use `FULL` if you always pull the entire set; switch to `INCREMENTAL` only once you have a robust delete-detection story (e.g., a "tombstone" field on the MDF policy).
- **Quota.** Inline `ImportIdentityMappings` caps at 500k entries per call; for very large orgs use the GCS source variant.

---

## Sources

- [Overview - Custom connector | Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-connector)
- [Create custom connector | Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/create-custom-connector)
- [Map external identities | Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/identity-mapping)
- [Prepare data for custom data sources | Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/prepare-data)
- [DataConnector REST reference | Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/reference/rest/v1/DataConnector)
- [Set up data source access control | Vertex AI Search](https://docs.cloud.google.com/generative-ai-app-builder/docs/data-source-access-control)
- [Document.Content (Python client) | Vertex AI Search](https://cloud.google.com/python/docs/reference/discoveryengine/0.10.0/google.cloud.discoveryengine_v1.types.Document.Content)
- [Respecting ACLs in Google AgentSpace Custom Data Source Searches](https://aashna-kunk.medium.com/agentspace-searches-on-custom-data-sources-respecting-acls-6b1a03fbc833)
- [Build a Custom Connector for Gemini Enterprise (Sascha Heyer)](https://medium.com/google-cloud/build-a-custom-connector-for-gemini-enterprise-ad3aab884645)
- [SAP SuccessFactors Attachment OData v2 reference](https://help.sap.com/docs/successfactors-platform/sap-successfactors-api-reference-guide-odata-v2/attachment)
- [How to export SuccessFactors attachments using OData API (SAP KBA 2295413)](https://userapps.support.sap.com/sap/support/knowledge/en/2295413)
- [SuccessFactors OData API: OAuth 2.0 Authentication (SAP KBA 3462403)](https://userapps.support.sap.com/sap/support/knowledge/en/3462403)
- [Migrating SAP SuccessFactors API calls from Basic Auth to OAuth 2.0](https://blogs.sap.com/2022/02/03/migrating-sap-successfactors-api-calls-from-basic-authentication-to-oauth-2.0/)
- [Overview of SAP SuccessFactors Workforce SCIM API](https://help.sap.com/docs/successfactors-platform/managing-user-information/overview-of-sap-successfactors-workforce-system-for-cross-domain-identity-management-api)
