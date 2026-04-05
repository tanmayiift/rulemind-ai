#!/usr/bin/env bash
set -euo pipefail

HOST="${RULEMIND_E2E_HOST:-127.0.0.1}"
PORT="${RULEMIND_E2E_PORT:-8080}"
TIMEOUT_SECONDS="${RULEMIND_E2E_HEALTH_TIMEOUT:-30}"

for ((i=0; i<TIMEOUT_SECONDS; i++)); do
  if curl -sf "http://${HOST}:${PORT}/health" >/dev/null; then
    echo "Backend healthy at http://${HOST}:${PORT}/health"
    exit 0
  fi
  sleep 1
done

echo "Timed out waiting for backend health at http://${HOST}:${PORT}/health" >&2
exit 1
