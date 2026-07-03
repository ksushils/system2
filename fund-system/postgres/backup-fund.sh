#!/bin/bash
# Nightly backup of fund.json — keeps last 14 days locally + timestamped copies
# Add to crontab: 0 2 * * * /root/fund-system/backup-fund.sh
set -e
SRC="/root/fund-system/data/fund.json"
BACKUP_DIR="/root/fund-system/backups"
mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d_%H%M%S)

# Copy with timestamp
cp "$SRC" "$BACKUP_DIR/fund_$TS.json"

# Keep only the last 14 days of backups
find "$BACKUP_DIR" -name "fund_*.json" -mtime +14 -delete

# Also keep a "latest" copy for quick restore
cp "$SRC" "$BACKUP_DIR/fund_latest.json"

echo "[$(date)] Backed up fund.json -> fund_$TS.json ($(du -h "$SRC" | cut -f1))"
