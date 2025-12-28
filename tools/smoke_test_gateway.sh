#!/usr/bin/env bash
set -euo pipefail

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing required command: $1" >&2
    exit 1
  }
}

require_cmd curl

BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:8800}"
TOKEN="${GATEWAY_BEARER_TOKEN:-}"

if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: GATEWAY_BEARER_TOKEN is not set" >&2
  echo "Usage: GATEWAY_BEARER_TOKEN=... $0" >&2
  echo "Optional: set GATEWAY_BASE_URL (default: ${BASE_URL})" >&2
  exit 1
fi

echo "Base URL: ${BASE_URL}"

echo "[1/3] GET /health"
curl -fsS "${BASE_URL}/health" >/dev/null

echo "[2/3] GET /v1/models"
curl -fsS "${BASE_URL}/v1/models" \
  -H "Authorization: Bearer ${TOKEN}" \
  >/dev/null

echo "[3/3] POST /v1/embeddings"
curl -fsS "${BASE_URL}/v1/embeddings" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model":"default","input":"smoke test"}' \
  >/dev/null

echo "OK: smoke tests passed"
