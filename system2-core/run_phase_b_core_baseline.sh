#!/usr/bin/env bash
set -uo pipefail

cd /root/system2-core
mkdir -p logs

stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log_file="/root/system2-core/logs/nightly.log"

{
  echo "[$stamp] START phase_b_core_baseline"
  if .venv/bin/python run_phase_b_core_baseline.py; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] SUCCESS phase_b_core_baseline"
    exit 0
  else
    rc=$?
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FAILURE phase_b_core_baseline exit=$rc"
    exit "$rc"
  fi
} >> "$log_file" 2>&1
