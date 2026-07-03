#!/usr/bin/env bash
set -euo pipefail

mkdir -p /root/system2-core/logs
echo "$(date -Is) starting System 2 pre-market gap check"

curl -sS \
  -X POST "http://127.0.0.1:3210/api/system2/pre-market-gap/run-local" \
  -H "Content-Type: application/json" \
  -d '{"dryRun":false,"thresholdAtrMultiple":1.5}'

echo
echo "$(date -Is) finished System 2 pre-market gap check"
