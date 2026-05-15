#!/usr/bin/env bash
# =====================================================================
# register_data_store.sh
# Registers the Snowflake MCP server as a Gemini Enterprise data store
# via the Discovery Engine REST API. Same thing the console does, but
# scripted so it can live in CI.
#
# Prereqs:
#   - gcloud auth application-default login
#   - roles/discoveryengine.editor on the project
#   - The org-policy constraint blocking custom MCP data stores has
#     been overridden for this project.
# =====================================================================
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
LOCATION="${LOCATION:-global}"
COLLECTION="${COLLECTION:-default_collection}"
DATA_STORE_ID="${DATA_STORE_ID:-snowflake-sales-mcp}"

MCP_URL="${MCP_URL:?set MCP_URL (from DESC MCP SERVER ...)}"
AUTH_URL="${AUTH_URL:?set AUTH_URL}"
TOKEN_URL="${TOKEN_URL:?set TOKEN_URL}"
CLIENT_ID="${CLIENT_ID:?set CLIENT_ID}"
CLIENT_SECRET="${CLIENT_SECRET:?set CLIENT_SECRET}"
SCOPES="${SCOPES:-offline_access}"

ACCESS_TOKEN="$(gcloud auth print-access-token)"

curl -sS -X POST \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${PROJECT_ID}/locations/${LOCATION}/collections/${COLLECTION}/dataStores?dataStoreId=${DATA_STORE_ID}" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @- <<EOF
{
  "displayName": "Snowflake Sales Warehouse",
  "industryVertical": "GENERIC",
  "solutionTypes": ["SOLUTION_TYPE_CHAT"],
  "contentConfig": "NO_CONTENT",
  "mcpServerConfig": {
    "serverUrl": "${MCP_URL}",
    "transport": "STREAMABLE_HTTP",
    "description": "Authoritative source for all sales/revenue/order analytics backed by the Snowflake sales warehouse.",
    "oauthConfig": {
      "authorizationUrl": "${AUTH_URL}",
      "tokenUrl":         "${TOKEN_URL}",
      "clientId":         "${CLIENT_ID}",
      "clientSecret":     "${CLIENT_SECRET}",
      "scopes":           "${SCOPES}"
    }
  }
}
EOF

echo
echo "Data store created. Now enable the 'sales_analyst' tool in the"
echo "Gemini Enterprise console (Actions tab) -- tools are disabled by default."
