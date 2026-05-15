"""
SuccessFactors OData client — fetches HR policy PDF documents along with
the role-based-permission groups that should be allowed to read each one.

SuccessFactors auth is OAuth 2.0 SAML-bearer (the standard for SF OData).
The flow is:
  1. Build a signed SAML assertion (issuer = OAuth client id, subject = user id,
     audience = SF token URL).
  2. POST it to the token URL with grant_type=urn:ietf:params:oauth:grant-type:saml2-bearer
     to receive a short-lived access token.
  3. Call SF OData with `Authorization: Bearer <token>`.

For brevity, the SAML assertion construction is sketched here; in production
use `python3-saml` or `cryptography` + a small assertion builder. The signing
key is loaded from Secret Manager so the connector never holds it on disk.

HR policy documents live in SuccessFactors Document Management Service (DMS),
exposed via the `DMSDocument` OData entity. We filter to categories tagged as
HR policies. Adjust the category list / filter expression for your tenant.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Iterator

import requests
from google.cloud import secretmanager

logger = logging.getLogger(__name__)

# Category id(s) in SuccessFactors DMS that hold HR policies. Get these from
# the SF admin who curates the policy library — they are tenant-specific.
HR_POLICY_CATEGORY_IDS = ("HR_POLICY", "HR_HANDBOOK", "BENEFITS_POLICY")


@dataclass
class PolicyDocument:
    """One HR policy as it comes out of SuccessFactors."""
    document_id: str                       # SF DMS documentId — STABLE primary key
    title: str
    description: str
    mime_type: str                         # almost always "application/pdf"
    body_bytes: bytes                      # the actual PDF content
    source_url: str                        # link back to SF for citation footer
    business_unit: str | None              # e.g. "EMEA"
    language: str | None                   # e.g. "en_US"
    effective_date: datetime | None
    expiration_date: datetime | None
    updated_at: datetime                   # SF lastModifiedDateTime — used for watermarking

    # Role-Based Permission groups in SuccessFactors (e.g. "EMEA_HR_MANAGERS").
    # These are the external identities we will reference in document ACLs.
    rbp_groups: list[str] = field(default_factory=list)
    # If the policy is marked as accessible to all employees in SF, this is True
    # and the document is exposed to every authenticated user (idp_wide).
    all_employees: bool = False


class SuccessFactorsClient:
    def __init__(self, host: str, company_id: str, token_url: str,
                 client_id: str, user_id: str, signing_key_secret: str):
        self.host = host
        self.company_id = company_id
        self.token_url = token_url
        self.client_id = client_id
        self.user_id = user_id
        self.signing_key_secret = signing_key_secret
        self._token: str | None = None
        self._session = requests.Session()

    # -- Auth ------------------------------------------------------------------

    def _signing_key(self) -> bytes:
        sm = secretmanager.SecretManagerServiceClient()
        return sm.access_secret_version(name=self.signing_key_secret).payload.data

    def _build_saml_assertion(self) -> str:
        """Build + sign a SAML assertion suitable for SF's saml2-bearer grant.

        Production note: use a real SAML library here. This stub exists so the
        OAuth flow below is wired correctly; replace with your tenant's
        assertion builder before deploying.
        """
        # signing_key = self._signing_key()
        # assertion_xml = build_signed_assertion(
        #     issuer=self.client_id,
        #     subject=self.user_id,
        #     audience=self.token_url,
        #     signing_key=signing_key,
        # )
        # return base64.b64encode(assertion_xml).decode()
        raise NotImplementedError(
            "Plug in your SAML assertion builder here. The SF tenant admin can "
            "share the exact issuer/audience/recipient values for your OAuth "
            "client registration."
        )

    def _get_token(self) -> str:
        if self._token:
            return self._token
        resp = self._session.post(
            self.token_url,
            data={
                "company_id": self.company_id,
                "client_id": self.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:saml2-bearer",
                "assertion": self._build_saml_assertion(),
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    # -- OData fetch -----------------------------------------------------------

    def _odata_get(self, path: str, params: dict) -> dict:
        url = f"https://{self.host}/odata/v2/{path}"
        params = {**params, "$format": "json"}
        resp = self._session.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._get_token()}"},
            timeout=60,
        )
        if resp.status_code == 401:
            # Token expired mid-run — refresh once and retry.
            self._token = None
            resp = self._session.get(
                url, params=params,
                headers={"Authorization": f"Bearer {self._get_token()}"}, timeout=60,
            )
        resp.raise_for_status()
        return resp.json()

    def fetch_hr_policy_documents(
        self, modified_since: datetime | None,
    ) -> Iterator[PolicyDocument]:
        """Paginate over DMSDocument, filtered to HR policy categories.

        SF OData paginates via $skip / $top. We use $top=200 which is the
        practical sweet spot — bigger pages risk SF gateway timeouts on PDF-
        heavy responses.
        """
        page_size = 200
        skip = 0
        category_filter = " or ".join(
            f"categoryId eq '{c}'" for c in HR_POLICY_CATEGORY_IDS
        )
        filter_expr = f"({category_filter}) and isActive eq true"
        if modified_since is not None:
            # SF expects /Date(ms)/ format in some endpoints; ISO-8601 in
            # others. For lastModifiedDateTime on DMSDocument the ISO form
            # works on recent tenants.
            filter_expr += (
                f" and lastModifiedDateTime gt datetimeoffset'"
                f"{modified_since.isoformat()}'"
            )

        while True:
            page = self._odata_get(
                "DMSDocument",
                params={
                    "$top": page_size,
                    "$skip": skip,
                    "$filter": filter_expr,
                    # Expand role-based permission groups linked to the doc.
                    # The actual nav-property name varies by tenant; confirm
                    # with the SF admin (often `permissions` or `rbpGroups`).
                    "$expand": "permissions",
                },
            )
            results = page.get("d", {}).get("results", [])
            if not results:
                return
            for row in results:
                yield self._row_to_policy(row)
            skip += len(results)
            if len(results) < page_size:
                return

    def _row_to_policy(self, row: dict) -> PolicyDocument:
        # Download the PDF body. SF returns a documentBody URL or an inline
        # base64 blob depending on entity; DMSDocument typically gives a URL.
        body_url = row.get("documentBodyUrl") or f"https://{self.host}/{row['documentBodyPath']}"
        body_resp = self._session.get(
            body_url,
            headers={"Authorization": f"Bearer {self._get_token()}"},
            timeout=120,
        )
        body_resp.raise_for_status()
        body_bytes = body_resp.content

        rbp_groups = [p["groupId"] for p in row.get("permissions", {}).get("results", []) if p.get("groupId")]
        all_employees = bool(row.get("isPublic")) or "ALL_EMPLOYEES" in rbp_groups

        return PolicyDocument(
            document_id=row["documentId"],
            title=row.get("title") or row.get("name") or row["documentId"],
            description=row.get("description") or "",
            mime_type=row.get("mimeType") or "application/pdf",
            body_bytes=body_bytes,
            source_url=f"https://{self.host}/sf/dms/document/{row['documentId']}",
            business_unit=row.get("businessUnit"),
            language=row.get("language"),
            effective_date=_parse_sf_date(row.get("effectiveDate")),
            expiration_date=_parse_sf_date(row.get("expirationDate")),
            updated_at=_parse_sf_date(row["lastModifiedDateTime"]) or datetime.utcnow(),
            rbp_groups=rbp_groups,
            all_employees=all_employees,
        )


def _parse_sf_date(value) -> datetime | None:
    """SF date columns come back as `/Date(1700000000000)/` or ISO-8601.
    Handle both."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value)
    if s.startswith("/Date("):
        ms = int(s[len("/Date("):-2].split("+")[0])
        return datetime.utcfromtimestamp(ms / 1000.0)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
