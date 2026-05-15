"""Configuration for the CMS -> Gemini Enterprise connector.

All values can be overridden via environment variables. Secrets (API keys,
service-account JSON paths) should come from a secret manager in production
(e.g. GCP Secret Manager). They are read here only for clarity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # --- Source CMS ---
    cms_base_url: str = os.getenv("CMS_BASE_URL", "https://api.internal/articles")
    cms_api_token: str = os.getenv("CMS_API_TOKEN", "")
    cms_page_size: int = int(os.getenv("CMS_PAGE_SIZE", "500"))
    cms_timeout_s: float = float(os.getenv("CMS_TIMEOUT_S", "30"))
    cms_max_retries: int = int(os.getenv("CMS_MAX_RETRIES", "5"))

    # --- Gemini Enterprise / Vertex AI Search (Discovery Engine) ---
    gcp_project_id: str = os.getenv("GCP_PROJECT_ID", "my-project")
    gcp_location: str = os.getenv("GCP_LOCATION", "global")
    datastore_id: str = os.getenv("DATASTORE_ID", "cms-articles")
    # "branch" is almost always "default_branch" for new connectors.
    branch_id: str = os.getenv("BRANCH_ID", "default_branch")

    # --- State / checkpointing ---
    # Cursor file or GCS URI for "last successful run" watermark.
    state_uri: str = os.getenv("STATE_URI", "gs://my-bucket/cms-connector/state.json")

    # --- Batching / concurrency ---
    import_batch_size: int = int(os.getenv("IMPORT_BATCH_SIZE", "100"))
    max_workers: int = int(os.getenv("MAX_WORKERS", "8"))

    # --- Run mode ---
    # "full" (initial seed) or "incremental" (hourly delta).
    run_mode: str = os.getenv("RUN_MODE", "incremental")


CONFIG = Config()
