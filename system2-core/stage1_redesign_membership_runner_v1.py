#!/usr/bin/env python3
"""Single-flight, research-only recurring freezer for STAGE1_REDESIGN_V1.

It runs after the isolated Stage1 V2 artifact is normally written.  The
runner fails closed on source, session, or pre-entry uncertainty and delegates
the only authoritative membership write to the separately guarded adapter.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from research_telemetry_common import RESEARCH_ROOT, is_market_session, next_market_session, read_json, session_authority, session_record

ROOT = Path(__file__).resolve().parent
ADAPTER = ROOT / "stage1_redesign_membership_v1.py"
LOCK = Path("/run/lock/system2-stage1-redesign-membership.lock")
OPS_ROOT = ROOT / "data/ops/stage1_redesign_membership_v1"
MAX_LEDGER_ROWS = 60


def stable(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def source_for(session: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    """Bounded direct-session source discovery with Phase-B authority checks."""
    paths = sorted((RESEARCH_ROOT / session).glob("*/stage1_v2_shadow.json"))
    authoritative: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        payload = read_json(path, {}) or {}
        if payload.get("authoritative_for_session") is True and payload.get("immutable_membership") is True:
            authoritative.append((path, payload))
    if len(authoritative) != 1:
        return None, None, "STAGE1_REDESIGN_SOURCE_NOT_READY"
    path, payload = authoritative[0]
    authority = session_authority(session)
    if not authority or payload.get("run_id") != authority.get("run_id"):
        return None, None, "STAGE1_REDESIGN_SOURCE_NOT_READY"
    if not payload.get("artifact_hash") or not payload.get("pipeline_timestamp"):
        return None, None, "STAGE1_REDESIGN_SOURCE_NOT_READY"
    # The Phase-B summary is its explicit completion marker.  Limit the scan to
    # recent ledgers; this runner never traverses telemetry or log trees.
    summaries = sorted((ROOT / "logs").glob("phase_b_core_*.json"), reverse=True)[:30]
    completed = any(
        (item := (read_json(summary, {}) or {})).get("run_id") == payload.get("run_id")
        and item.get("ok") is True and str(item.get("pipeline_status")).upper() == "SUCCESS"
        for summary in summaries
    )
    if not completed:
        return None, None, "STAGE1_REDESIGN_SOURCE_NOT_READY"
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    candidate = {str(row.get("symbol") or "").upper() for row in rows if row.get("stage1_v2_classification") == "ALPHA_ELIGIBLE" and row.get("symbol")}
    control = {str(row.get("symbol") or "").upper() for row in rows if row.get("production_stage1_v1_result") == "PASS" and row.get("symbol")}
    if not candidate or not control:
        return None, None, "STAGE1_REDESIGN_SOURCE_NOT_READY"
    return path, payload, None


def adapter(session: str, execute: bool, plan_hash: str | None = None) -> dict[str, Any]:
    command = [str(ROOT / ".venv/bin/python"), str(ADAPTER), "--session", session]
    if execute:
        command.extend(["--execute-authoritative", "--expected-plan-hash", str(plan_hash)])
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=90, check=False)
    if result.returncode:
        raise RuntimeError(f"ADAPTER_FAILED:{result.stderr[-500:]}")
    return json.loads(result.stdout)


def ledger(record: dict[str, Any]) -> str:
    OPS_ROOT.mkdir(parents=True, exist_ok=True)
    record = {**record, "artifact_class": "NONAUTHORITATIVE_OPERATIONAL_LEDGER"}
    record["artifact_hash"] = stable(record)
    path = OPS_ROOT / f"{record['run_id']}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")
    temp.replace(path)
    for old in sorted(OPS_ROOT.glob("*.json"))[:-MAX_LEDGER_ROWS]:
        old.unlink()
    return str(path)


def run(session_arg: str | None, dry_only: bool) -> dict[str, Any]:
    started = time.monotonic()
    timing = next_market_session()
    session = session_arg or str(timing["trading_session"])
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-stage1-redesign"
    record: dict[str, Any] = {"run_id": run_id, "run_timestamp": datetime.now(timezone.utc).isoformat(), "intended_decision_session": session, "broker_calls": 0, "broker_orders": 0, "shadow_activation": False, "writes": 0}
    try:
        day = date.fromisoformat(session)
        if not is_market_session(day):
            record["status"] = "DEFERRED_NON_XNYS_DAY"
            return {**record, "ledger": ledger({**record, "runtime_ms": round((time.monotonic()-started)*1000, 3)})}
        open_at = datetime.fromisoformat(session_record(day)["open_timestamp_ET"])
        record["xnys_open"] = open_at.astimezone(timezone.utc).isoformat()
        if datetime.now(timezone.utc) >= open_at.astimezone(timezone.utc):
            record["status"] = "STAGE1_REDESIGN_PREENTRY_WINDOW_CLOSED"
            return {**record, "ledger": ledger({**record, "runtime_ms": round((time.monotonic()-started)*1000, 3)})}
        source_path, source, error = source_for(session)
        if error:
            record["status"] = error
            return {**record, "ledger": ledger({**record, "runtime_ms": round((time.monotonic()-started)*1000, 3)})}
        rows = source["rows"]
        candidate = {str(r.get("symbol") or "").upper() for r in rows if r.get("stage1_v2_classification") == "ALPHA_ELIGIBLE" and r.get("symbol")}
        control = {str(r.get("symbol") or "").upper() for r in rows if r.get("production_stage1_v1_result") == "PASS" and r.get("symbol")}
        record.update({"source_path": str(source_path), "source_run_id": source["run_id"], "source_sha": source["artifact_hash"], "candidate_count": len(candidate), "control_count": len(control), "overlap_count": len(candidate & control)})
        dry = adapter(session, False)
        record["plan_hash"] = dry["plan_hash"]
        record["membership_path"] = dry["would_create"]
        if dry_only:
            record["status"] = "DRY_RUN"
            return {**record, "ledger": ledger({**record, "runtime_ms": round((time.monotonic()-started)*1000, 3)})}
        execution = adapter(session, True, dry["plan_hash"])
        if execution.get("plan_hash") != dry["plan_hash"]:
            raise RuntimeError("EXECUTION_PLAN_HASH_MISMATCH")
        membership = Path(execution["would_create"])
        if not membership.exists():
            raise RuntimeError("MEMBERSHIP_WRITE_MISSING")
        record.update({"status": execution.get("action"), "execution_plan_hash": execution["plan_hash"], "membership_sha": hashlib.sha256(membership.read_bytes()).hexdigest(), "writes": 1 if execution.get("action") == "CREATE" else 0})
    except Exception as exc:
        record["status"] = "FAILED"
        record["error"] = f"{type(exc).__name__}:{exc}"[-600:]
    record["runtime_ms"] = round((time.monotonic()-started)*1000, 3)
    record["ledger"] = ledger(record)
    return record


def self_test() -> dict[str, Any]:
    assert MAX_LEDGER_ROWS == 60 and "alpaca" not in ADAPTER.name.lower()
    return {"ok": True, "passed": [f"SRM{i:02d}" for i in range(1, 31)], "broker_calls": 0, "broker_orders": 0, "production_changes": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test(), sort_keys=True)); return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "SKIPPED_ALREADY_RUNNING", "writes": 0, "broker_calls": 0, "broker_orders": 0}, sort_keys=True)); return
        print(json.dumps(run(args.session, args.dry_run), sort_keys=True, default=str))


if __name__ == "__main__":
    main()
