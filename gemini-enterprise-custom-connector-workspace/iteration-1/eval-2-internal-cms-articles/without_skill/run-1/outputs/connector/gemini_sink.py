"""Discovery Engine (Gemini Enterprise) sink.

Uses the Google Cloud Discovery Engine Python client. For ~50k docs with
hourly updates, an inline import per batch is the simplest correct approach.
For full-backfill you may prefer staging to GCS and calling
`import_documents` with a GCS source (faster, cheaper).
"""

from __future__ import annotations

import logging
from typing import Iterable, List

from google.api_core import retry as gapi_retry
from google.cloud import discoveryengine_v1 as discoveryengine

from .config import CONFIG

log = logging.getLogger(__name__)


class GeminiSink:
    def __init__(self) -> None:
        # Regional endpoint for non-global locations.
        client_options = (
            {"api_endpoint": f"{CONFIG.gcp_location}-discoveryengine.googleapis.com"}
            if CONFIG.gcp_location != "global"
            else None
        )
        self._client = discoveryengine.DocumentServiceClient(client_options=client_options)
        self._parent = self._client.branch_path(
            project=CONFIG.gcp_project_id,
            location=CONFIG.gcp_location,
            data_store=CONFIG.datastore_id,
            branch=CONFIG.branch_id,
        )

    # ---- upserts ------------------------------------------------------------
    @gapi_retry.Retry(predicate=gapi_retry.if_transient_error)
    def upsert_batch(self, docs: List[dict]) -> None:
        """Inline import (upsert) for a batch of documents."""
        if not docs:
            return

        de_docs = [
            discoveryengine.Document(
                id=d["id"],
                schema_id=d.get("schemaId", "default_schema"),
                content=discoveryengine.Document.Content(
                    mime_type=d["content"]["mimeType"],
                    raw_bytes=__import__("base64").b64decode(d["content"]["rawBytes"]),
                ),
                struct_data=_to_struct(d.get("structData", {})),
            )
            for d in docs
        ]

        request = discoveryengine.ImportDocumentsRequest(
            parent=self._parent,
            inline_source=discoveryengine.ImportDocumentsRequest.InlineSource(documents=de_docs),
            reconciliation_mode=discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL,
            auto_generate_ids=False,
            id_field="id",
        )
        op = self._client.import_documents(request=request)
        log.info("submitted import op=%s docs=%d", op.operation.name, len(de_docs))
        # Block on the LRO so we surface errors immediately in the hourly job.
        result = op.result(timeout=900)
        log.info("import op finished: %s", result)

    # ---- deletes ------------------------------------------------------------
    @gapi_retry.Retry(predicate=gapi_retry.if_transient_error)
    def delete_ids(self, ids: Iterable[str]) -> None:
        for doc_id in ids:
            name = self._client.document_path(
                project=CONFIG.gcp_project_id,
                location=CONFIG.gcp_location,
                data_store=CONFIG.datastore_id,
                branch=CONFIG.branch_id,
                document=doc_id,
            )
            try:
                self._client.delete_document(name=name)
                log.info("deleted doc id=%s", doc_id)
            except Exception as e:  # 404 is fine; surface anything else
                log.warning("delete failed id=%s err=%s", doc_id, e)


def _to_struct(d: dict):
    """Convert a python dict into a protobuf Struct for struct_data."""
    from google.protobuf import struct_pb2
    s = struct_pb2.Struct()
    s.update(d)
    return s
