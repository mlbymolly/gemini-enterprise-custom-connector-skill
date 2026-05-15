# SAP recipe

"SAP" covers a lot of ground — the right pattern depends entirely on which SAP system and what data.

## Pick the path

| SAP source | Data shape | Right path |
|---|---|---|
| SuccessFactors (HR documents, policies, employee forms) | Mostly stable documents | **Ingestion connector** |
| S/4HANA business data (orders, invoices, master data) | Transactional, real-time-sensitive | **MCP server** |
| SAP Knowledge Central / Help portals | Static-ish HTML knowledge | **Ingestion connector** |
| SAP Datasphere / BW warehouse | Analytics tables | **MCP server** (treat like Snowflake) |
| SAP Concur / Ariba / Fieldglass (Business Suite) | Transactional with documents attached | Usually **both** — MCP for the transactional API, ingestion for attached documents |

When in doubt, ask the user which SAP system they mean. "SAP" alone isn't enough to design against.

## SAP auth — picking a model

SAP authentication is its own world. The connector you build depends on the SAP integration surface:

- **OData / REST APIs (SuccessFactors, S/4HANA Cloud, Ariba)**: OAuth 2.0. Same model as the MCP OAuth flow in `mcp-server.md`. Easiest.
- **SAP Gateway / on-prem OData**: usually BasicAuth or X.509 client certs. Run the MCP server in a VPC with Serverless VPC Access to reach the on-prem gateway.
- **RFC / BAPI (on-prem ERP)**: SAP JCo or pyrfc. No OAuth — service account credentials with SAP user/password, stored in Secret Manager. The MCP server acts as the single SAP user; ACL enforcement happens in SAP authorization objects.
- **SAP Datasphere / BW**: SAML or OAuth depending on tenant. Use the official Python client.

## Identity mapping is almost always required

SAP user IDs (`P12345`, `SAP_USER_X`) are not Google Workspace emails. For the ingestion path, you almost certainly need an Identity Mapping Store mapping SAP user IDs to Google identities. Build this mapping table early — without it, ACL enforcement won't work, and you'll discover this only after end-to-end testing fails.

For the MCP path, you can either:
- Forward the Google identity through and look up the SAP user from a mapping table inside the MCP server, then call SAP as that user (preferred).
- Call SAP as a service account and enforce ACLs in your MCP server logic (only acceptable if SAP-side roles are too coarse to be useful).

## Ingestion-specific notes (SuccessFactors / SAP knowledge bases)

- Documents are often binary (PDF policies, DOCX forms). Pass bytes to `content.raw_bytes` with the correct `mime_type` — Discovery Engine extracts text.
- Metadata to surface in `struct_data`: document type (policy / form / SOP), business unit, effective date, expiration date, language.
- SuccessFactors documents have ACLs encoded as **role-based permissions** (e.g., "EMEA HR Managers"). Map these to Google Groups and reference them as `group_id` in document ACLs.
- Use the SAP system's modification timestamp as the watermark for incremental sync. Don't rely on document creation date — documents get updated.

## MCP-specific notes (S/4HANA, Datasphere)

- Tool surface for S/4HANA: `get_order(order_id)`, `list_orders(customer_id, date_range)`, `get_invoice(invoice_id)`, etc. One tool per business object — generic `run_query` is wrong because S/4HANA isn't SQL.
- Tool surface for Datasphere: `run_query(sql)` is appropriate — same shape as Snowflake.
- Heavy SAP responses (full BAPI returns) can be massive. Filter and reshape inside the MCP server before returning — agents don't need the full payload, just the fields relevant to the question.

## SAP-specific guardrails

- **Concurrent session limits**: SAP enforces per-user concurrent connection caps. If the MCP server uses a single service account, you'll hit these fast. Pool connections aggressively.
- **License implications**: each SAP user that the connector acts as may count against an SAP user license. Confirm with the user's SAP licensing team before scoping.
- **Transport rules**: pulling production data through a non-SAP-blessed channel may violate the user's SAP change-management policy. Worth flagging — better to find out early than after a deploy is blocked.

## When in doubt

Ask the user:
1. Which SAP product? (SuccessFactors, S/4HANA, Ariba, Concur, BW, Datasphere, on-prem ECC?)
2. Cloud or on-prem? (Determines the integration surface — OData vs. RFC.)
3. What's the use case — agents reading documents, looking up transactions, running analytics?

Then map their answers to the table at the top.
