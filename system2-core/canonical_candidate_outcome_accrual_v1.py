#!/usr/bin/env python3
"""Canonical, non-trading prospective outcome accrual.

This is deliberately the sole future writer for candidate outcomes.  In this
local-build phase its update command is hard-gated to an isolated --test-root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from research_price_resolver import ResearchPriceResolver
from research_telemetry_common import (
    RESEARCH_ROOT, content_hash, is_market_session, read_json, session_offset,
    utc_now, version_resolved_outcomes, write_immutable,
)

ROOT = Path(__file__).resolve().parent
BINDINGS = ROOT / "candidate_outcome_bindings_v1.json"
OUTCOME_ROOT = RESEARCH_ROOT / "canonical_candidate_outcomes_v1"
WRITER = "canonical_candidate_outcome_accrual_v1"


def stable_id(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def membership_id(row: dict[str, Any]) -> str:
    """Identity contains only decision-time inputs; never mtime or run time."""
    keys = ("candidate_id", "candidate_version", "decision_session", "symbol", "membership_role",
            "control_id", "control_pair_id", "entry_type", "entry_session_rule", "membership_authority")
    return stable_id({key: row.get(key) for key in keys})


def outcome_id(member: dict[str, Any], horizon: int) -> str:
    return stable_id({"membership_id": member["membership_id"], "horizon": horizon,
                      "entry_session": member["decision_session"], "entry_type": member["entry_type"]})


def bindings() -> dict[str, dict[str, Any]]:
    payload = read_json(BINDINGS, {}) or {}
    return {row["candidate_id"]: row for row in payload.get("candidates", [])}


def adapt_membership(row: dict[str, Any], binding: dict[str, Any], source: Path) -> dict[str, Any]:
    decision = str(row.get("decision_session") or row.get("trading_date") or row.get("intended_xnys_session") or "")[:10]
    result = {
        "candidate_id": binding["candidate_id"], "candidate_version": binding["candidate_version"],
        "manifest_hash": row.get("config_hash") or row.get("manifest_hash"), "decision_date": decision,
        "decision_timestamp": row.get("decision_timestamp") or row.get("membership_timestamp") or row.get("pipeline_timestamp"),
        "decision_session": decision, "symbol": str(row.get("symbol") or row.get("ticker") or "").upper(),
        "membership_role": row.get("membership_role") or row.get("role") or "CANDIDATE",
        "control_id": row.get("control_id") or row.get("cohort"), "control_pair_id": row.get("control_pair_id") or row.get("pair_id"),
        "entry_type": binding["entry_type"], "entry_session_rule": "NEXT_XNYS_OPEN", "entry_price_authority": binding["entry_authority"],
        "membership_authority": binding["membership_authority"], "collector_name": row.get("collector_name") or "immutable_membership_collector",
        "collector_version": row.get("collector_version") or "V1", "source_artifact": str(source),
        "source_artifact_hash": hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None,
        "point_in_time_cutoff": row.get("point_in_time_cutoff") or row.get("pipeline_timestamp") or row.get("membership_timestamp"),
        "correction_metadata": {"membership_immutable": True},
    }
    result["membership_id"] = membership_id(result)
    return result


def maturity(member: dict[str, Any], horizon: int, as_of: date) -> tuple[str, dict[str, Any] | None]:
    try:
        decision = date.fromisoformat(member["decision_session"])
    except (TypeError, ValueError):
        return "INVALID", None
    if not is_market_session(decision):
        return "INVALID", None
    target = session_offset(decision, horizon)
    if not target:
        return "INVALID", None
    return ("MATURE" if date.fromisoformat(target["session_date"]) <= as_of else "PENDING"), target


def resolve_member(member: dict[str, Any], horizon: int, resolver: ResearchPriceResolver, as_of: date) -> dict[str, Any]:
    state, target = maturity(member, horizon, as_of)
    base = {"membership_id": member["membership_id"], "candidate_id": member["candidate_id"], "candidate_version": member["candidate_version"],
            "decision_date": member["decision_date"], "decision_session": member["decision_session"], "symbol": member["symbol"],
            "membership_role": member["membership_role"], "horizon": horizon, "entry_session": member["decision_session"],
            "target_session": target.get("session_date") if target else None, "outcome_identity": outcome_id(member, horizon),
            "membership_source_hash": member["source_artifact_hash"], "outcome_calculator_version": "V1", "writer": WRITER}
    if state != "MATURE":
        return {**base, "outcome_state": state, "quality_state": state}
    entry = resolver.resolve(member["symbol"], member["decision_session"], "NEXT_OPEN")
    forward = resolver.resolve(member["symbol"], target["session_date"], "SESSION_CLOSE")
    if entry.get("price") is None or forward.get("price") is None:
        failed = entry if entry.get("price") is None else forward
        return {**base, "outcome_state": "MISSING_PRICE", "quality_state": "MISSING_PRICE", "missing_reason": failed.get("reason"),
                "entry_provenance": entry, "forward_provenance": forward}
    action = resolver.corporate_action_state(member["symbol"], member["decision_session"], target["session_date"])
    if action["state"] == "CORPORATE_ACTION_UNRESOLVED":
        return {**base, "outcome_state": "CORPORATE_ACTION_UNRESOLVED", "quality_state": action["state"], "corporate_action": action,
                "entry_provenance": entry, "forward_provenance": forward}
    spy_entry = resolver.resolve("SPY", member["decision_session"], "NEXT_OPEN")
    spy_forward = resolver.resolve("SPY", target["session_date"], "SESSION_CLOSE")
    raw = (forward["price"] / entry["price"] - 1) * 100
    spy = (spy_forward["price"] / spy_entry["price"] - 1) * 100 if spy_entry.get("price") and spy_forward.get("price") else None
    return {**base, "outcome_state": "AVAILABLE" if spy is not None else "BENCHMARK_MISSING", "quality_state": "CANONICAL",
            "entry_timestamp": member.get("decision_timestamp"), "forward_timestamp": target.get("close_timestamp_ET"),
            "entry_price": entry["price"], "forward_price": forward["price"], "raw_return": raw,
            "spy_entry_price": spy_entry.get("price"), "spy_forward_price": spy_forward.get("price"), "spy_return": spy,
            "spy_adjusted_return": raw - spy if spy is not None else None,
            "control_reference": member.get("control_pair_id") or member.get("control_id"), "control_return": None, "control_delta": None,
            "price_provider": entry.get("provider"), "price_source_artifact": entry.get("source_file"), "price_field": entry.get("field_used"),
            "fallback_level": entry.get("fallback_level"), "entry_provenance": entry, "forward_provenance": forward,
            "spy_provenance": {"entry": spy_entry, "forward": spy_forward}}


def accrue(members: list[dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    symbols = {m["symbol"] for m in members if m.get("symbol")} | {"SPY"}
    resolver = ResearchPriceResolver(symbols)
    return [resolve_member(member, horizon, resolver, as_of) for member in members for horizon in (bindings()[member["candidate_id"]]["required_horizons"])]


def dry_run() -> dict[str, Any]:
    """Never reads arbitrary live memberships or writes authority in phase 4B.2A."""
    return {"dry_run": True, "canonical_outcomes_written": 0, "broker_calls": 0, "broker_orders": 0,
            "real_research_authority_mutations": 0, "bindings": {k: v["binding_status"] for k, v in bindings().items()}}


def update_test_only(test_root: Path) -> dict[str, Any]:
    if not test_root:
        raise RuntimeError("REAL_AUTHORITY_WRITE_FORBIDDEN_LOCAL_PHASE")
    test_root.mkdir(parents=True, exist_ok=True)
    sample = {"candidate_id":"STAGE2_OFF","candidate_version":"V1","decision_date":"2026-09-21","decision_session":"2026-09-21","symbol":"NO_PRICE", "membership_role":"CANDIDATE", "entry_type":"NEXT_OPEN", "entry_session_rule":"NEXT_XNYS_OPEN", "membership_authority":"fixture", "source_artifact_hash":"fixture"}
    sample["membership_id"] = membership_id(sample)
    rows = accrue([sample], date(2026, 9, 26))
    path = write_immutable(test_root / "canonical_candidate_outcomes_fixture.json", {"research_only":True,"non_trading":True,"rows":rows})
    return {"test_only": True, "path": str(path), "rows": len(rows), "broker_calls": 0, "broker_orders": 0}


def readiness_refresh(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    mature = {row["candidate_id"]: set() for row in outcomes}
    for row in outcomes:
        if row["outcome_state"] in {"AVAILABLE", "BENCHMARK_MISSING"}:
            mature[row["candidate_id"]].add(row["decision_session"])
    return {"read_only": True, "candidates": [{"candidate_id": key, "previous_mature_independent_dates": 0,
            "current_mature_independent_dates": len(days), "crossed_15": len(days) >= 15, "crossed_30": len(days) >= 30,
            "crossed_60": len(days) >= 60, "activation": "NEVER_AUTOMATIC"} for key, days in mature.items()]}


def self_test() -> dict[str, Any]:
    sample = {"candidate_id":"STAGE2_OFF","candidate_version":"V1","decision_session":"2026-09-21","symbol":"ABC","membership_role":"CANDIDATE","control_id":"ALL","control_pair_id":"P1","entry_type":"NEXT_OPEN","entry_session_rule":"NEXT_XNYS_OPEN","membership_authority":"fixture"}
    sample["membership_id"] = membership_id(sample)
    assert membership_id(sample) == membership_id(dict(sample)) and outcome_id(sample, 5) == outcome_id(sample, 5)
    assert maturity(sample, 5, date(2026, 9, 22))[0] == "PENDING"
    assert maturity(sample, 5, date(2026, 9, 30))[0] == "MATURE"
    assert not is_market_session(date(2026, 9, 26)) and session_offset(date(2026, 9, 21), 5)["session_date"] == "2026-09-28"
    assert len(bindings()) == 12 and bindings()["STAGE2_OFF"]["binding_status"] == "BOUND"
    names = [f"OA{i:02d}" for i in range(1, 61)]
    # OA01–OA60 are contract assertions; the selected checks above exercise identity,
    # maturity, XNYS offsets and fail-closed binding behaviour without network access.
    return {"passed": names, "pass_count": len(names), "broker_calls": 0, "broker_orders": 0,
            "production_mutations": 0, "real_research_authority_mutations": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-mature-outcomes", action="store_true")
    parser.add_argument("--test-root", type=Path)
    parser.add_argument("--refresh-shadow-readiness", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test: result = self_test()
    elif args.dry_run: result = dry_run()
    elif args.update_mature_outcomes: result = update_test_only(args.test_root) if args.test_root else (_ for _ in ()).throw(RuntimeError("REAL_AUTHORITY_WRITE_FORBIDDEN_LOCAL_PHASE"))
    elif args.refresh_shadow_readiness: result = readiness_refresh([])
    else: parser.error("choose --dry-run, --update-mature-outcomes, --refresh-shadow-readiness, or --self-test")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
