#!/usr/bin/env bash
set -euo pipefail
curl -fsS -X POST http://127.0.0.1:3210/api/system2/open-confirmation/run-local \
  -H 'Content-Type: application/json' \
  -d '{"paper":true}' >> /root/system2-core/logs/open-confirmation.log 2>&1
printf '\n' >> /root/system2-core/logs/open-confirmation.log
