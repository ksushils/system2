#!/usr/bin/env bash
set -uo pipefail

cd /root/system2-core
mkdir -p logs

stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log_file="/root/system2-core/logs/nightly.log"

{
  echo "[$stamp] START phase_b_core_baseline"
  before_latest="$(ls -t logs/phase_b_core_*.json 2>/dev/null | head -1 || true)"
  .venv/bin/python run_phase_b_core_baseline.py
  rc=$?
  latest="$(ls -t logs/phase_b_core_*.json 2>/dev/null | head -1 || true)"
  status="NO_PHASE_JSON"
  ok="false"
  if [ -n "$latest" ] && [ "$latest" != "$before_latest" ]; then
    parsed="$(
      .venv/bin/python - "$latest" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    print(str(data.get("pipeline_status") or data.get("status") or "UNKNOWN"))
    print("true" if data.get("ok") is True else "false")
except Exception as exc:
    print(f"PARSE_ERROR:{exc}")
    print("false")
PY
    )"
    status="$(printf '%s\n' "$parsed" | sed -n '1p')"
    ok="$(printf '%s\n' "$parsed" | sed -n '2p')"
  fi
  status_upper="$(printf '%s' "$status" | tr '[:lower:]' '[:upper:]')"
  if [ "$rc" -eq 0 ] && [ "$status_upper" = "SUCCESS" ] && [ "$ok" = "true" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] SUCCESS phase_b_core_baseline status=$status ok=$ok json=$latest"
    exit 0
  else
    if [ "$rc" -eq 0 ]; then rc=1; fi
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FAILURE phase_b_core_baseline exit=$rc status=$status ok=$ok json=${latest:-none}"
    exit "$rc"
  fi
} >> "$log_file" 2>&1
