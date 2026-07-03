#!/usr/bin/env bash
set -uo pipefail

mkdir -p /root/system2-core/logs
log_file="/root/system2-core/logs/paper-monitor.log"

{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] START paper_monitor"
  if curl -s -X POST http://127.0.0.1:3210/api/system2/monitor/run-local; then
    echo
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] SUCCESS paper_monitor"
    exit 0
  else
    rc=$?
    echo
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FAILURE paper_monitor exit=$rc"
    exit "$rc"
  fi
} >> "$log_file" 2>&1
