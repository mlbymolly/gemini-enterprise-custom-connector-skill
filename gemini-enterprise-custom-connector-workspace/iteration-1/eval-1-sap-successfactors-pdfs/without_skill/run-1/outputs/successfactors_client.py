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
