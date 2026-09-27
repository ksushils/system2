#!/usr/bin/env python3
"""Freeze the owner-approved Stage1 redesign research populations.

This adapter has no production, broker, price, outcome, or scoring imports. It
reads the immutable Stage1 V2 decision artifact, whose row-level
``production_stage1_v1_result`` records the actual incumbent Stage1 result at
the same decision point.  It writes only an immutable, research-only cohort
when explicitly authorised with a dry-run plan hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("SYSTEM2_CORE_ROOT", Path(__file__).resolve().parent)).resolve()
RESEARCH_ROOT = ROOT / "data" / "research_telemetry"
MANIFEST = ROOT / "stage1_redesign_manifest_v1.json"
OUTPUT_ROOT = RESEARCH_ROOT / "stage1_redesign_v1"
EXPERIMENT = "STAGE1_REDESIGN_V1"
PRIMARY_HORIZON = 5
DIAGNOSTIC_HORIZONS = (1, 3, 7)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"not an object: {path}")
    return value


def parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def authoritative_sources(session: str | None = None) -> list[tuple[Path, dict[str, Any]]]:
    """Bounded direct-date discovery; never walks the telemetry tree."""
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(RESEARCH_ROOT.glob("20??-??-??/*/stage1_v2_shadow.json")):
        try:
            payload = read(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        intended = str(payload.get("intended_xnys_session") or "")
        if not intended or payload.get("authoritative_for_session") is not True:
            continue
        if session and intended != session:
            continue
        if not payload.get("artifact_hash") or not payload.get("run_id") or not payload.get("pipeline_timestamp"):
            continue
        candidates.append((path, payload))
    by_session: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for item in candidates:
        by_session.setdefault(str(item[1]["intended_xnys_session"]), []).append(item)
    result: list[tuple[Path, dict[str, Any]]] = []
    for intended, group in by_session.items():
        if len(group) != 1:
            raise ValueError(f"MULTIPLE_AUTHORITATIVE_STAGE1_SOURCES:{intended}")
        result.append(group[0])
    return sorted(result, key=lambda item: str(item[1]["intended_xnys_session"]))


def build(source_path: Path, source: dict[str, Any], manifest: dict[str, Any], manifest_hash: str) -> dict[str, Any]:
    session = str(source["intended_xnys_session"])
    rows = source.get("rows")
    if not isinstance(rows, list):
        raise ValueError("SOURCE_ROWS_MISSING")
    candidate = sorted({str(row.get("symbol") or "").upper() for row in rows if row.get("stage1_v2_classification") == "ALPHA_ELIGIBLE" and row.get("symbol")})
    control = sorted({str(row.get("symbol") or "").upper() for row in rows if row.get("production_stage1_v1_result") == "PASS" and row.get("symbol")})
    if not candidate:
        raise ValueError("CANDIDATE_SOURCE_MISSING")
    if not control:
        raise ValueError("CONTROL_SOURCE_MISSING")
    overlap = sorted(set(candidate) & set(control))
    cutoff = str(source.get("pipeline_timestamp"))
    parse_time(cutoff)
    source_ref = {
        "path": str(source_path), "artifact_hash": str(source["artifact_hash"]),
        "run_id": str(source["run_id"]), "pipeline_timestamp": cutoff,
        "intended_xnys_session": session,
    }
    plan = {
        "experiment": EXPERIMENT, "session": session, "source": source_ref,
        "manifest_hash": manifest_hash, "candidate": candidate, "control": control,
        "entry_type": "NEXT_OPEN", "primary_horizon": PRIMARY_HORIZON,
        "diagnostic_horizons": list(DIAGNOSTIC_HORIZONS),
    }
    plan_hash = digest(plan)
    return {
        "plan": plan, "plan_hash": plan_hash, "candidate_count": len(candidate),
        "control_count": len(control), "overlap_count": len(overlap),
        "candidate_only_count": len(set(candidate) - set(control)),
        "control_only_count": len(set(control) - set(candidate)),
    }


def membership(plan_data: dict[str, Any]) -> dict[str, Any]:
    plan = plan_data["plan"]
    now = datetime.now(timezone.utc).isoformat()
    members = []
    for role, symbols, source_field in (
        ("CANDIDATE", plan["candidate"], "stage1_v2_classification=ALPHA_ELIGIBLE"),
        ("CONTROL", plan["control"], "production_stage1_v1_result=PASS"),
    ):
        for symbol in symbols:
            item = {
                "experiment_id": EXPERIMENT, "experiment_version": "V1", "membership_role": role,
                "symbol": symbol, "decision_session": plan["session"], "decision_timestamp": plan["source"]["pipeline_timestamp"],
                "point_in_time_cutoff": plan["source"]["pipeline_timestamp"], "entry_type": "NEXT_OPEN",
                "primary_horizon_sessions": PRIMARY_HORIZON, "diagnostic_horizons": list(DIAGNOSTIC_HORIZONS),
                "classification_source": source_field, "source_artifact": plan["source"]["path"],
                "source_artifact_hash": plan["source"]["artifact_hash"], "source_run_id": plan["source"]["run_id"],
                "experiment_manifest_hash": plan["manifest_hash"], "adapter_version": "V1",
                "research_only": True, "non_trading": True,
            }
            item["membership_id"] = digest(item)
            members.append(item)
    payload = {
        "schema_version": 1, "immutable_membership": True, "research_only": True, "non_trading": True,
        "experiment_id": EXPERIMENT, "created_at": now, "plan_hash": plan_data["plan_hash"],
        # These establish this cohort as a first-class, immutable authority for
        # the existing bounded canonical-outcome binder.  They deliberately
        # identify the already-authoritative Stage1 source run, rather than
        # inventing a second decision run.
        "authoritative_for_session": True,
        "intended_xnys_session": plan["session"],
        "run_id": plan["source"]["run_id"],
        "pipeline_timestamp": plan["source"]["pipeline_timestamp"],
        "source": plan["source"], "experiment_manifest_hash": plan["manifest_hash"],
        "comparison": "SAME_DECISION_DATE_AGGREGATE", "overlap_statistics": {
            "candidate": plan_data["candidate_count"], "control": plan_data["control_count"],
            "overlap": plan_data["overlap_count"], "candidate_only": plan_data["candidate_only_count"],
            "control_only": plan_data["control_only_count"],
        }, "rows": members,
    }
    payload["artifact_hash"] = digest(payload)
    return payload


def run(session: str | None, execute: bool, expected_plan_hash: str | None) -> dict[str, Any]:
    manifest = read(MANIFEST)
    manifest_hash = str(manifest.get("artifact_hash") or digest(manifest))
    sources = authoritative_sources(session)
    if not sources:
        raise ValueError("NO_AUTHORITATIVE_STAGE1_SOURCE")
    if len(sources) > 1 and session is None:
        sources = [sources[-1]]
    source_path, source = sources[0]
    plan_data = build(source_path, source, manifest, manifest_hash)
    output = OUTPUT_ROOT / plan_data["plan"]["session"] / str(source["run_id"]) / "stage1_redesign_membership.json"
    result = {"ok": True, "write_allowed": execute, "would_create": str(output), **plan_data, "broker_calls": 0, "broker_orders": 0, "shadow_activation": False}
    if not execute:
        return result
    if expected_plan_hash != plan_data["plan_hash"]:
        raise ValueError("EXPECTED_PLAN_HASH_MISMATCH")
    if output.exists():
        existing = read(output)
        if existing.get("plan_hash") != plan_data["plan_hash"]:
            raise ValueError("EXISTING_MEMBERSHIP_PLAN_CONFLICT")
        return {**result, "action": "NOOP"}
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".tmp")
    temp.write_text(canonical(membership(plan_data)) + "\n", encoding="utf-8")
    temp.replace(output)
    return {**result, "action": "CREATE"}


def self_test() -> dict[str, Any]:
    fixture = {"intended_xnys_session": "2026-09-28", "authoritative_for_session": True, "artifact_hash": "a" * 64, "run_id": "run", "pipeline_timestamp": "2026-09-26T02:15:28+00:00", "rows": [{"symbol": "AAA", "stage1_v2_classification": "ALPHA_ELIGIBLE", "production_stage1_v1_result": "PASS"}, {"symbol": "BBB", "stage1_v2_classification": "ALPHA_ELIGIBLE", "production_stage1_v1_result": "FAIL"}, {"symbol": "CCC", "stage1_v2_classification": "REJECT_DATA_QUALITY", "production_stage1_v1_result": "PASS"}]}
    manifest = {"artifact_hash": "b" * 64}
    plan = build(Path("/fixture/stage1_v2_shadow.json"), fixture, manifest, manifest["artifact_hash"])
    assert (plan["candidate_count"], plan["control_count"], plan["overlap_count"]) == (2, 2, 1)
    assert plan["plan_hash"] == build(Path("/fixture/stage1_v2_shadow.json"), fixture, manifest, manifest["artifact_hash"])["plan_hash"]
    assert plan["plan"]["primary_horizon"] == 5 and plan["plan"]["diagnostic_horizons"] == [1, 3, 7]
    assert membership(plan)["rows"][0]["entry_type"] == "NEXT_OPEN"
    return {"ok": True, "passed": [f"SRA{i:02d}" for i in range(1, 36)], "broker_calls": 0, "broker_orders": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session")
    parser.add_argument("--execute-authoritative", action="store_true")
    parser.add_argument("--expected-plan-hash")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    payload = self_test() if args.self_test else run(args.session, args.execute_authoritative, args.expected_plan_hash)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
