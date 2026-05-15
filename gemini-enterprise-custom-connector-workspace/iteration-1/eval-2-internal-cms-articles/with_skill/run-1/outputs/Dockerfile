FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "google-cloud-discoveryengine>=0.13.0" \
    "google-cloud-storage>=2.16.0" \
    "requests>=2.32.0"

COPY connector.py source_fetcher.py transform.py identity_mapping.py ./
COPY infra ./infra

# Cloud Run Jobs entrypoint.
CMD ["python", "connector.py"]
