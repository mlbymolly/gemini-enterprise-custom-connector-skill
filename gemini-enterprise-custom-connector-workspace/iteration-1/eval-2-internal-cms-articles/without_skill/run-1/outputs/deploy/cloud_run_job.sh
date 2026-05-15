#!/usr/bin/env bash
# Deploy as a Cloud Run Job + hourly Cloud Scheduler trigger.
# Requires: gcloud auth, project + region set, Artifact Registry repo "connectors".
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/connectors/cms-gemini:latest"
JOB="cms-gemini-connector"
SA="cms-gemini-connector@${PROJECT_ID}.iam.gserviceaccount.com"

# Build + push
gcloud builds submit --tag "${IMAGE}" .

# Create or update the job
gcloud run jobs deploy "${JOB}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --service-account "${SA}" \
  --max-retries 1 \
  --task-timeout 30m \
  --cpu 1 --memory 1Gi \
  --set-env-vars "GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=global,DATASTORE_ID=cms-articles,RUN_MODE=incremental,STATE_URI=gs://${PROJECT_ID}-connector-state/cms/state.json,CMS_BASE_URL=https://api.internal/articles" \
  --set-secrets "CMS_API_TOKEN=cms-api-token:latest"

# Hourly schedule
gcloud scheduler jobs create http "${JOB}-hourly" \
  --schedule "0 * * * *" \
  --time-zone "Etc/UTC" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB}:run" \
  --http-method POST \
  --oauth-service-account-email "${SA}" \
  || gcloud scheduler jobs update http "${JOB}-hourly" --schedule "0 * * * *"
