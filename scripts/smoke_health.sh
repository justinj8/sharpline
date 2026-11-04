#!/usr/bin/env bash
# Poll BASE_URL/v1/health until it reports ok (or give up after 120 s).
# Usage: scripts/smoke_health.sh https://api.example.com
set -euo pipefail
BASE_URL="${1:?usage: smoke_health.sh BASE_URL}"
deadline=$((SECONDS + 120))
body=""
while (( SECONDS < deadline )); do
  body="$(curl -fsS --max-time 10 "${BASE_URL%/}/v1/health" 2>/dev/null || true)"
  if [[ "$body" == *'"status":"ok"'* ]]; then
    echo "healthy: $body"
    exit 0
  fi
  sleep 5
done
echo "unhealthy after 120s; last response: ${body:-<none>}" >&2
exit 1
