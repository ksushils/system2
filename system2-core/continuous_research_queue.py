#!/usr/bin/env python3
"""Immutable, non-trading System2 research implementation queue."""
from __future__ import annotations
import argparse
import json
from typing import Any
from research_telemetry_common import RESEARCH_ROOT, read_json, utc_now, write_immutable

ROOT = RESEARCH_ROOT / "continuous_research_queue_v1"
REGISTRY = ROOT / "registry.json"
REPORTS = ROOT / "reports"
STATUSES = {"COLLECTING", "EARLY_EVIDENCE", "PRELIMINARY", "REVIEWABLE",
            "READY_FOR_SHADOW_V2", "READY_FOR_PRODUCTION_REVIEW", "REJECTED", "IMPLEMENTED"}
ACTIONS = {"NO", "SHADOW_ONLY", "READY_FOR_OWNER_REVIEW"}

def strategy(item: str, hypothesis: str, challenger: str, metric: str, control: str,
             threshold: str, evidence: str) -> dict[str, Any]:
    return {"item": item, "type": "STRATEGY_RESEARCH", "hypothesis": hypothesis,
            "current_production_behavior": "Production strategy remains unchanged.",
            "challenger_definition": challenger, "primary_metric": metric,
            "matched_control": control, "required_evidence_threshold": threshold,
            "current_independent_dates": "UNKNOWN", "status": "COLLECTING",
            "evidence": evidence, "primary_result": "NOT_MATURED",
            "next_checkpoint": "30 independent XNYS dates (preliminary only)",
            "implement_now": "NO",
            "reason": "Research-only; owner review is required before any production change."}

def initial() -> dict[str, Any]:
    s = [
        strategy("STAGE2_SIMPLIFIED_V2", "Existing Stage2 features with mature positive incremental IC may rank 3-5D abnormal return better.", "Frozen equal signed ranks of only existing, point-in-time features with demonstrated positive incremental value; no invented features.", "+5D SPY-adjusted return and score IC", "FULL_STAGE2_QUARTILES_V1 and STAGE1_NEXT_OPEN_BASELINE_V1", ">=30 dates preliminary; >=60 dates plus positive chronological halves and matched-control improvement before review.", "Await mature feature-IC evidence; no formula is authorized."),
        strategy("STAGE2_OFF_CONTROL", "Stage2 selection adds value versus the unfiltered Stage1 baseline.", "Identical next-open outcomes for Stage2 population versus Stage1 baseline.", "+5D SPY-adjusted retained-population difference", "STAGE1_NEXT_OPEN_BASELINE_V1", ">=30 dates preliminary; >=60 and stable halves for review.", "Uses existing baseline/quartile collection; no duplicate cohort."),
        strategy("CLUSTER_OFF_CONTROL", "Cluster keeping improves outcomes over the same Stage1/Stage2 population without cluster selection.", "Cluster-kept names versus matched non-cluster/rejected names with identical next-open entry and horizons.", "Cluster-kept minus matched control +5D SPY-adjusted", "CLUSTER_KEPT_NEXT_OPEN_V1 / STAGE2_REJECTED_BY_CLUSTER_V1 where available", ">=30 paired dates preliminary; >=60 paired dates and stable halves for review.", "Existing champion/control artifacts are authority; missing pairing is reported, never inferred."),
        strategy("FINALIST_OFF_CONTROL", "Finalist selection adds value over all eligible Stage2 names under identical next-open measurement.", "Immutable paired comparison: all Stage2 eligible names versus finalists.", "Finalists minus all-eligible +5D SPY-adjusted", "FULL_STAGE2_QUARTILES_V1", ">=30 paired dates preliminary; >=60 paired dates and stable halves for review.", "Requires explicit prospective pairing; historic membership is never inferred from current data."),
        strategy("PEAD_EXIT_V2", "A small pre-defined exit comparison may improve realized PEAD outcome without changing its validated entry.", "Only FIXED_HOLD, CURRENT_EXIT, TP1_BREAKEVEN, and ATR_TIME_EXIT on the same prospective PEAD events.", "Resolved R on matched PEAD events", "PEAD_NEXT_OPEN_FIXED_HOLD_V1 and PEAD_TP1_BREAKEVEN_SHADOW_V1", ">=30 resolved matched events and >=60 dates before owner review; no parameter search.", "Existing PEAD shadows collect first; no exit change is authorized."),
        strategy("PMF_EARLY_ENTRY_V2", "Early entry may improve executable PMF outcomes, but PMF V1 remains retired.", "Prospective early-entry shadow only; V2 cannot be considered until executable improvement is demonstrated.", "+3D/+5D abnormal return and executable-cost-adjusted outcome", "PMF_EARLY_ENTRY_SHADOW_V1 and retired PMF reference", ">=30 dates, positive cost-adjusted result, and stable halves before any shadow-V2 proposal.", "No broker entry path; PMF V1 retirement is invariant."),
        strategy("EXECUTION_COST_MODEL_V2", "Measured execution cost can make research outcomes more realistic without changing signals.", "Versioned observed-cost model evaluated against retained fills and quote-quality telemetry only.", "Error versus observed fill/slippage and cost-adjusted research return", "Fixed-cost assumptions in current shadow definitions", ">=30 comparable fills/quotes across >=30 dates before review.", "Collection/calibration only; it never changes broker orders."),
    ]
    infra = [
        {"item":"TEST_HARNESS_ISOLATION","type":"INFRASTRUCTURE","status":"IMPLEMENTED","evidence":"Previously deployed isolation work; retain for regression monitoring.","current_independent_dates":"N/A","primary_result":"NON_TRADING_HARNESS","next_checkpoint":"Regression check after test/runtime changes","implement_now":"NO","reason":"Already implemented; monitor only."},
        {"item":"TRADE_ID_SEQUENCE","type":"INFRASTRUCTURE","status":"COLLECTING","evidence":"Retained queue item; no automatic evidence evaluator.","current_independent_dates":"N/A","primary_result":"NOT_APPLICABLE","next_checkpoint":"Next identity/reconciliation audit","implement_now":"NO","reason":"Safety work, not alpha."},
        {"item":"DISK_RETENTION","type":"INFRASTRUCTURE","status":"EARLY_EVIDENCE","evidence":"Retention is monitored; disk headroom remains an operational metric.","current_independent_dates":"N/A","primary_result":"MONITORING","next_checkpoint":"Weekly disk and retention review","implement_now":"NO","reason":"No automatic deletion decision."},
        {"item":"PERSISTENCE_V2_IF_NEEDED","type":"INFRASTRUCTURE","status":"COLLECTING","evidence":"Persistence V1 is awaiting a complete post-fix XNYS soak.","current_independent_dates":"N/A","primary_result":"POST_FIX_SOAK_PENDING","next_checkpoint":"Complete full-session post-fix soak","implement_now":"NO","reason":"No V2 without a measured defect."},
        {"item":"CREDENTIAL_ROTATION","type":"INFRASTRUCTURE","status":"REVIEWABLE","evidence":"Historical repository exposure was identified; values are intentionally not recorded.","current_independent_dates":"N/A","primary_result":"ROTATION_REQUIRED","next_checkpoint":"Owner-approved rotation plan","implement_now":"READY_FOR_OWNER_REVIEW","reason":"Security remediation requires controlled external credential rotation."}]
    return {"schema_version":1, "created_at":utc_now().isoformat(), "research_only":True, "non_trading":True,
            "promotion_policy":"No production strategy change is automatic. Rejected items remain permanently in rejected_history.",
            "active_production_components":["STAGE1_V1","STAGE2_V1","CLUSTER_SELECTION_V1","FINALIST_SELECTION_V1","PEAD","PMF_V1_RETIRED","PMF_RESEARCH_STAMPING","PMF_POSITION_RECONCILIATION_OCO","RESEARCH_MEASUREMENT_V2","PERSISTENCE_MEMORY_FIX_V1"],
            "active_shadow_experiments":["PEAD_NEXT_OPEN_FIXED_HOLD_V1","STAGE1_NEXT_OPEN_BASELINE_V1","FULL_STAGE2_QUARTILES_V1","NEXT_OPEN_CHASE_CONTEXT_V1","PEAD_TP1_BREAKEVEN_SHADOW_V1","PMF_EARLY_ENTRY_SHADOW_V1","MOMENTUM_CONTROL_V1","CATALYST_CONTINUATION_V1","IDIOSYNCRATIC_REVERSAL_V1"],
            "proposed_strategy_items":s, "infrastructure_items":infra, "rejected_history":[]}

def ensure() -> dict[str, Any]:
    prior = read_json(REGISTRY, None)
    if isinstance(prior, dict): return prior
    ROOT.mkdir(parents=True, exist_ok=True)
    write_immutable(REGISTRY, initial())
    return read_json(REGISTRY, {}) or {}

def validate(registry: dict[str, Any]) -> list[str]:
    errors = []
    for section in ("proposed_strategy_items", "infrastructure_items"):
        for row in registry.get(section, []):
            if row.get("status") not in STATUSES: errors.append("invalid status: " + str(row.get("item")))
            if row.get("implement_now") not in ACTIONS: errors.append("invalid action: " + str(row.get("item")))
    return errors

def weekly() -> str:
    registry = ensure()
    rows = registry.get("proposed_strategy_items", [])
    payload = {"generated_at":utc_now().isoformat(),"title":"SYSTEM2 IMPLEMENTATION QUEUE",
               "research_only":True,"validation_errors":validate(registry),
               "columns":["ITEM","TYPE","CURRENT STATUS","EVIDENCE","INDEPENDENT DATES","PRIMARY RESULT","NEXT CHECKPOINT","IMPLEMENT NOW?","REASON"],
               "top_three":[{"item":x["item"],"reason":"NO_MATURE_PRIMARY_EVIDENCE — queue visibility only."} for x in rows[:3]],
               "strategy_items":rows,"infrastructure_items":registry.get("infrastructure_items",[]),
               "rejected_history":registry.get("rejected_history",[])}
    return str(write_immutable(REPORTS / ("implementation_queue_" + utc_now().strftime("%Y%m%dT%H%M%SZ") + ".json"), payload))

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init","weekly","self-test"))
    args = parser.parse_args()
    registry = ensure()
    result = {"registry":str(REGISTRY),"broker_calls":0,"production_changes":0}
    if args.command == "weekly": result["report"] = weekly()
    if args.command == "self-test": result["errors"] = validate(registry)
    print(json.dumps(result))
if __name__ == "__main__": main()
