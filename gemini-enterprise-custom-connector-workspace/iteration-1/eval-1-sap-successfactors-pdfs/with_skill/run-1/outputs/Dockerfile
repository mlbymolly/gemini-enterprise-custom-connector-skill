FROM python:3.12-slim

WORKDIR /app

# System certs for SF + Workday TLS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "google-cloud-discoveryengine>=0.13.0" \
        "google-cloud-storage>=2.16.0" \
        "google-cloud-secret-manager>=2.20.0" \
        "requests>=2.32.0"

COPY connector.py source_fetcher.py transform.py identity_mapping.py ./

# Cloud Run Jobs ignore PORT; entrypoint just runs and exits.
ENTRYPOINT ["python", "connector.py"]
