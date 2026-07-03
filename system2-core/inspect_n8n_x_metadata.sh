#!/usr/bin/env sh
set -eu
PATTERN='GETXAPI|api.getxapi|Twitter|tweet|X Social Momentum|TWITTER|APIFY|RAPIDAPI|X_BEARER'
for file in /home/node/.n8n/database.sqlite /home/node/.n8n/database.sqlite-wal; do
  echo "FILE: $file"
  if [ -f "$file" ]; then
    strings "$file" | grep -Ei "$PATTERN" | head -120 || true
  fi
done
