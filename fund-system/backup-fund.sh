#!/bin/bash
# Nightly backup of fund.json + Postgres — keeps last 14 days
set -e
SRC="/root/fund-system/data/fund.json"
BACKUP_DIR="/root/fund-system/backups"
mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d_%H%M%S)

# 1. Backup fund.json
cp "$SRC" "$BACKUP_DIR/fund_$TS.json"
cp "$SRC" "$BACKUP_DIR/fund_latest.json"

# 2. Backup Postgres (if available)
if docker ps | grep -q localrank-postgres; then
  docker exec localrank-postgres pg_dump -U postgres -d fund_system > "$BACKUP_DIR/fund_postgres_$TS.sql" 2>/dev/null && \
    echo "[$(date)] Postgres dumped -> fund_postgres_$TS.sql" || \
    echo "[$(date)] Postgres dump failed (continuing)"
  # Keep only last 14 days of SQL dumps
  find "$BACKUP_DIR" -name "fund_postgres_*.sql" -mtime +14 -delete
fi

# 3. Keep only last 14 days of JSON backups
find "$BACKUP_DIR" -name "fund_*.json" -mtime +14 -delete

echo "[$(date)] Backed up fund.json -> fund_$TS.json ($(du -h "$SRC" | cut -f1))"
