#!/bin/bash
# Health monitor — pings the fund-system API and alerts if degraded/down
# Add to crontab: */5 * * * * /root/fund-system/health-monitor.sh

URL="http://127.0.0.1:3210/api/health"
LOG="/root/fund-system/logs/health-monitor.log"
mkdir -p "$(dirname "$LOG")"

# Check if service is responding
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$URL" 2>/dev/null || echo "000")
TS=$(date -Iseconds)

if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "503" ]; then
  echo "[$TS] CRITICAL: fund-system down (HTTP $HTTP_CODE)" >> "$LOG"
  # Try to restart
  pm2 restart fund-system >> "$LOG" 2>&1
  echo "[$TS] Restart attempted" >> "$LOG"
  exit 1
fi

# Parse health JSON
HEALTH_JSON=$(curl -s "$URL")
STATUS=$(printf '%s' "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','unknown'))" 2>/dev/null || echo "unknown")
ISSUES=$(printf '%s' "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d.get('issues',[])))" 2>/dev/null || echo "unknown")

if [ "$STATUS" = "degraded" ]; then
  echo "[$TS] WARNING: degraded — $ISSUES" >> "$LOG"
else
  echo "[$TS] OK: $STATUS" >> "$LOG"
fi

# System2 production heartbeat. It owns its own alert throttle and never
# changes trading data or decisions.
/root/system2-core/.venv/bin/python /root/system2-core/system2_heartbeat_alert.py >> "$LOG" 2>&1

# Rotate log if > 10MB
if [ -f "$LOG" ] && [ $(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG" 2>/dev/null || echo 0) -gt 10485760 ]; then
  mv "$LOG" "$LOG.old"
fi
