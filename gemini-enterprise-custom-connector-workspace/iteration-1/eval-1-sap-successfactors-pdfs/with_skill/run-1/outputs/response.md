# Gemini Enterprise connector for SAP SuccessFactors HR policy PDFs

## TL;DR — what we're building

A **Custom Connector → Discovery Engine datastore** (ingestion path). It pulls HR policy PDFs out of SuccessFactors DMS, pushes them into a Discovery Engine datastore with `acl_enabled=True`, and uses an **Identity Mapping Store (IMS)** to bridge SAP user IDs / SuccessFactors role-based permission groups to the Google identities your users sign in with through Workday SSO.

The Gemini Enterprise agent then queries that datastore and returns grounded answers with citations back to the original SF policy URLs.

## Why this path (and not an MCP server)

Walking the decision tree in the skill:

1. **Current state or snapshot?** — HR policies are stable documents. A periodically refreshed snapshot is fine. (Ingestion.)
2. **Write back to the source?** — No, agent only needs to read. (Either path.)
3. **Grounded with citations across other corpora?** — Yes, the whole point is "agent can cite HR policies." (Ingestion.)

Three-for-three on ingestion. The SAP recipe in the skill (`references/source-recipes/sap.md`) corroborates: SuccessFactors policies / forms are explicitly the "ingestion connector" row of the SAP path table.

## Assumptions I'm making (call these out if any are wrong)

1. **PDFs live in SuccessFactors DMS** (Document Management Service). Accessible through OData `DMSDocument` entity. If your policies actually live in **Employee Central** attachments, **JAM**, or **SAP Knowledge Central**, the fetch endpoint changes, but everything else in this design is identical.
2. **SF auth = OAuth 2.0 SAML-bearer.** This is the standard SF OData auth model. You'll need an OAuth client registered in SF Admin Center, a technical user with read on the HR-policy DMS categories, and the SAML signing key in Secret Manager.
3. **Workday already provisions parallel Google Groups** in Workspace for each SF role-based-permission group (e.g. the SF group `EMEA_HR_MANAGERS` has a matching `emea-hr-managers@yourco.com` group in Google). If you don't have that provisioning loop yet, **set it up first** — without it there's nothing to map SF RBP groups *to*, and ACL enforcement won't work end to end.
4. **Workday holds the SAP-user-id ↔ Google-email mapping** as a custom field on Worker (typical when Workday is the SSO source of truth). The connector pulls this on every run and pushes it into the IMS.
5. **Volumes are modest** — hundreds to low thousands of policies, not millions. The plain Python connector here is fine. If you're closer to 100k+ docs or need sub-hour freshness, switch to the BigQuery-CDC pattern in `references/enterprise-scale.md`.
6. **Connector runs on Cloud Run Jobs** with workload identity. Cloud Scheduler kicks it off.

## Architecture

```
SuccessFactors DMS (HR policy PDFs)
        │  OAuth 2.0 (SAML-bearer) — paginated OData reads
        ▼
   [ Cloud Run Job: connector.py ]
        │  1. Refresh IMS from Workday (user + group mappings)
        │  2. Pull policies modified since watermark
        │  3. Transform → discoveryengine.Document (PDF bytes + ACL)
        ▼
   [ JSONL shards on GCS staging bucket ]
        │  import_documents (FULL on first run, INCREMENTAL after)
        ▼
   [ Discovery Engine datastore — acl_enabled=True, IMS attached ]
        │  attached to
        ▼
   [ Gemini Enterprise app — grounded answers + citations ]
```

## ACL design (the part that's tricky here)

Your statement "SAP user IDs don't match Google emails" is exactly the case the IMS exists to solve. Two flavors of mapping you need:

| External identity in SF                       | Mapped to                            | Where it's referenced in document ACLs |
|-----------------------------------------------|--------------------------------------|----------------------------------------|
| SAP user id (e.g. `P12345`)                   | Google email (`alice@yourco.com`)    | `user_id: external_user:P12345`        |
| SF role-based-permission group (e.g. `EMEA_HR_MANAGERS`) | Google Group (`emea-hr-managers@yourco.com`) | `group_id: external_group:EMEA_HR_MANAGERS` |
| "All employees" policies                      | n/a                                  | `idp_wide: True`                       |

**Asymmetry to remember:** when you *import* mappings into the IMS, the external identity is bare (`"EMEA_HR_MANAGERS"`). When you *reference* that identity in a document's ACL, you prefix it (`"external_group:EMEA_HR_MANAGERS"`). `identity_mapping.py` and `transform.py` handle this correctly — but it bites everyone the first time.

For most HR policies you'll be ACL'ing to groups, not individuals, so the group mapping is the hot path. The user mapping is there for the occasional policy ACL'd to specific people (e.g. an executive comp policy).

## Setup order — do not reorder these

The IMS and `acl_enabled` are both **immutable** at datastore creation. If you skip or get them wrong, your only recovery is delete-and-recreate. So:

1. **Create the IMS first.**
2. **Create the datastore second**, pointing at the IMS, with `acl_enabled=True`.
3. **Then** load mappings and import documents.

`infra/create_datastore.py` does steps 1 and 2 idempotently.

## Project layout

```
sap-successfactors-hr-connector/
├── pyproject.toml
├── Dockerfile
├── connector.py            # Cloud Run Job entrypoint (fetch → transform → sync)
├── source_fetcher.py       # SuccessFactors OData client (OAuth 2.0 SAML-bearer)
├── transform.py            # PolicyDocument → discoveryengine.Document
├── identity_mapping.py     # Workday → IMS sync
└── infra/
    └── create_datastore.py # One-time: create IMS + datastore
```

The actual files are in this same `outputs/` directory.

## Key design choices, called out

**Stable document IDs.** Every doc uses `id = f"sf:dms:{documentId}"`. SF's `documentId` is the DMS primary key, so re-syncs upsert cleanly instead of duplicating. Never use `uuid4()` here — that's the most common cause of duplicate-document bugs at sync #2.

**PDF bytes go in as `content.raw_bytes` with `mime_type="application/pdf"`.** Discovery Engine does the text extraction server-side, so we don't need to OCR or text-extract in the connector.

**`struct_data` carries citation metadata** — `source_url`, `title`, `business_unit`, `effective_date`, `expiration_date`, `language`. These end up in citation footers and as filterable facets in the Gemini Enterprise app.

**Watermarking.** Stored as an ISO timestamp in a GCS object (`watermarks/successfactors_hr_policies.txt`). First run is `FULL`; subsequent runs query SF with `lastModifiedDateTime gt {watermark}` and use `INCREMENTAL`. Losing the watermark just means the next run re-syncs everything — annoying but not catastrophic.

**Deletes.** `INCREMENTAL` imports never delete. Two options:
- (Recommended for HR policies, since they're small.) Run a weekly `FULL` reconciliation by setting `SYNC_MODE=FULL` on a separate Cloud Scheduler trigger. SF returns every active policy, missing IDs get purged automatically.
- Or compute the diff in the connector and call `purge_documents(filter="id: ANY(...)")` for removed ones.

**GCS sharding.** 200 docs per JSONL file. Stays well under the 100MB per-file limit (with `dataSchema=content`) even with hefty PDFs, and well under the 100-files-per-request cap even at thousands of policies.

**Refresh IMS on every run.** Done at the top of `connector.py`. A new hire's policies are only readable once their SAP-id ↔ email mapping is in the IMS; group membership changes (someone joins EMEA HR) only take effect once Workspace has updated the group AND the RBP group mapping is current. Refreshing before the document import means the very same run can serve a new hire's first-day query.

## Wiring up the Gemini Enterprise app

After the first successful import:

1. Cloud Console → **Gemini Enterprise → Apps → Create app** (or open an existing one).
2. **Data stores → Add → Existing → `sap-hr-policies`** (or whatever `DATASTORE_ID` you used).
3. Save. Ask a test question as a known user; verify citations link back to SF DMS URLs and that ACL filtering is working (a user without the EMEA_HR_MANAGERS group shouldn't see EMEA-only policies).

A useful early sanity check: temporarily flag one doc as `idp_wide=True` in `transform.py`. If that doc surfaces in queries but the ACL'd ones don't, the indexing is fine — the issue is the IMS mapping. That's the right place to start debugging.

## Service account / IAM

Connector service account needs:

- `roles/discoveryengine.editor` on the project.
- `roles/storage.objectAdmin` on the staging GCS bucket.
- `roles/secretmanager.secretAccessor` on the SF SAML signing key secret and (whatever you use for) the Workday ISU credentials.
- Workday: an Integration System User with the Get_Workers permission and visibility on the custom SAP-user-id field.
- SuccessFactors: an OAuth client registered in SF Admin Center, plus a technical user assigned to the RBP role that grants read on the HR-policy DMS categories.

Run on Cloud Run with workload identity rather than a service account key.

## What to watch out for (SAP-specific guardrails the recipe calls out)

- **SF concurrent-session limits.** The connector acts as a single technical user. Don't fan out parallel jobs against the same user or you'll hit SF's per-user concurrent-connection cap. The single-job design here keeps you safely below it.
- **SAP licensing.** Confirm with your SAP licensing team that a technical/integration user is the right model — sometimes these count against named-user licenses.
- **Transport / change-management policy.** Pulling production HR docs through a new channel may need a formal SAP transport approval. Worth checking with the SAP team *before* you wire up production credentials.
- **Default-deny ACL bug.** If a policy has neither RBP groups attached nor an "all employees" flag, the transform will produce a document with an empty principals list — meaning **nobody** can read it. That's a known sharp edge; either log a warning at transform time or add a tenant-wide fallback group (`ALL_HR_POLICY_VIEWERS`) for safety.

## Files in this output

- `connector.py` — Cloud Run Job entrypoint
- `source_fetcher.py` — SuccessFactors OData / OAuth client (with stub for the SAML assertion — plug in your tenant's signer)
- `transform.py` — PolicyDocument → discoveryengine.Document with correct ACL prefixing
- `identity_mapping.py` — Workday → IMS sync for user + group mappings (Workday calls stubbed; plug in your client)
- `infra/create_datastore.py` — one-time IMS + datastore creation
- `Dockerfile` — Cloud Run Job image
- `pyproject.toml` — Python dependencies

## Open questions to confirm before you ship

1. **Which SF entity actually holds your policies** — `DMSDocument`, an `Attachment` on a custom MDF object, or something the team built? This decides the OData fetch in `source_fetcher.py`.
2. **What are the SF category IDs for HR policy content?** Currently stubbed as `HR_POLICY`, `HR_HANDBOOK`, `BENEFITS_POLICY` in `source_fetcher.py`.
3. **Does Workday already provision matching Google Groups for SF RBP groups?** If not, that provisioning loop is a prerequisite before any of this is useful.
4. **Custom field name on Workday Worker that stores the SAP/SF user id.** Needed for `identity_mapping.fetch_workday_user_mappings`.
5. **Refresh cadence** — every 15 min, hourly, daily? HR policies don't change often, so hourly INCREMENTAL + weekly FULL is a sensible default.
