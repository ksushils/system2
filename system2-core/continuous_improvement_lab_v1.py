#!/usr/bin/env python3
"""Append-only prospective controls and reports for the System2 improvement lab.

No broker/execution modules are imported.  Membership is frozen once per
authoritative XNYS session; outcomes use only Research Measurement V2.
"""
from __future__ import annotations
import argparse, hashlib, json, random, statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from research_telemetry_common import (RESEARCH_ROOT, authority_fields,
    independent_membership_rows, next_market_session, read_json, run_directory,
    session_authority, session_offset, utc_now, version_resolved_outcomes, write_immutable)
from research_price_resolver import ResearchPriceResolver
from swing_shadow_cohorts import SECTOR_ETFS, label, number, spearman

ROOT = Path(__file__).resolve().parent
LAB = RESEARCH_ROOT / "continuous_improvement_lab_v1"
HORIZONS = (1, 3, 5, 7)
EXPERIMENTS = ("STAGE2_OFF_CONTROL_V1", "CLUSTER_OFF_CONTROL_V1", "FINALIST_OFF_CONTROL_V1")

def symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()

def source_hash(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in paths: h.update(path.read_bytes() if path.exists() else b"MISSING")
    return h.hexdigest()

def fields(row: dict[str, Any], experiment: str, cohort: str, session: str, timing: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    return {"experiment":experiment, "cohort":cohort, "symbol":symbol(row),
            "trading_date":session, "next_open_timestamp":timing["next_session_open"],
            "entry_source":"NEXT_REGULAR_SESSION_OPEN", "membership_timestamp":utc_now().isoformat(),
            "stage2_score":number(row.get("setupQualityScore") or row.get("setup_score")),
            "sector":row.get("sector"), "market_cap":number(row.get("marketCap")),
            "dollar_volume":number(row.get("dollarVolume")), "average_volume":number(row.get("averageVolume")),
            "feature_snapshot":{"atr5MinPct":number(row.get("atr5MinPct")),
                "positive_extension_pct":max(0, number(row.get("distanceFromVWAP") if row.get("distanceFromVWAP") is not None else row.get("distanceFromVWMA"))) if number(row.get("distanceFromVWAP") if row.get("distanceFromVWAP") is not None else row.get("distanceFromVWMA")) is not None else None,
                "rsVsSpy":number(row.get("rsVsSpy")), "sectorAlpha":number(row.get("sectorAlpha"))},
            "outcome_state":"PENDING", **run}

def deterministic_cluster_control(kept: list[dict[str, Any]], rejected: list[dict[str, Any]], session: str) -> list[dict[str, Any]]:
    """No-reuse, deterministic nearest liquidity/cap/sector control from Stage2 rejects."""
    rng = random.Random(int(hashlib.sha256((session+"|CLUSTER_OFF_CONTROL_V1").encode()).hexdigest()[:16], 16))
    available = list(rejected); rng.shuffle(available); result = []
    for row in sorted(kept, key=symbol):
        sector, cap, vol = row.get("sector"), number(row.get("marketCap")), number(row.get("dollarVolume"))
        def distance(other: dict[str, Any]) -> tuple[float, str]:
            penalty = 0 if other.get("sector") == sector else 1_000_000
            ocap, ovol = number(other.get("marketCap")), number(other.get("dollarVolume"))
            cap_dist = abs((ocap or 0)-(cap or 0))/max(cap or ocap or 1, 1)
            vol_dist = abs((ovol or 0)-(vol or 0))/max(vol or ovol or 1, 1)
            return (penalty + cap_dist + vol_dist, symbol(other))
        if available:
            pick = min(available, key=distance); available.remove(pick); result.append(pick)
    return result

def create() -> dict[str, Any]:
    timing = next_market_session(); session = timing["trading_session"]; authority = session_authority(session)
    if not authority: return {"ok":False, "reason":"AUTHORITY_MISMATCH", "session":session}
    directory = run_directory(session, str(authority["run_id"]) + "-improvement-lab")
    target = directory / "improvement_lab_membership.json"
    if target.exists(): return {"ok":True, "idempotent":True, "path":str(target)}
    stage1 = [x for x in (read_json(ROOT/"stage1_survivors.json",[]) or []) if symbol(x)]
    stage2 = [x for x in (read_json(ROOT/"stage2_surgical_strike_top40.json",[]) or []) if symbol(x)]
    kept = [x for x in (read_json(ROOT/"stage7_clustered_survivors.json",[]) or []) if symbol(x)]
    rejected = [x for x in (read_json(ROOT/"stage7_cluster_rejections.json",[]) or []) if symbol(x)]
    if not stage1 or not stage2: return {"ok":False, "reason":"SOURCE_ERROR_EMPTY_STAGE", "session":session}
    pipeline = datetime.fromtimestamp((ROOT/"stage7_clustered_survivors.json").stat().st_mtime, timezone.utc).isoformat()
    run = authority_fields(session, authority["run_id"], pipeline)
    run["config_hash"] = source_hash([ROOT/"b3_surgical_strike_stage2.py", ROOT/"b4_correlation_cluster_engine.py", ROOT/"system2-config.json"])
    rows = []
    for exp, cohort, population in (
        ("STAGE2_OFF_CONTROL_V1","STAGE1_ALL",stage1), ("STAGE2_OFF_CONTROL_V1","STAGE2_SELECTED",stage2),
        ("CLUSTER_OFF_CONTROL_V1","CLUSTER_KEPT",kept), ("CLUSTER_OFF_CONTROL_V1","MATCHED_STAGE2_CONTROL",deterministic_cluster_control(kept,rejected,session)),
        ("FINALIST_OFF_CONTROL_V1","ALL_PRE_FINALIST_ELIGIBLE",stage2), ("FINALIST_OFF_CONTROL_V1","FINALISTS",kept)):
        rows.extend(fields(r, exp, cohort, session, timing, run) for r in population)
    payload = {"schema_version":1,"research_only":True,"non_trading":True,"immutable_membership":True,
               "rule_version":"CONTINUOUS_IMPROVEMENT_LAB_V1","deployment_timestamp":utc_now().isoformat(),
               "intended_xnys_session":session,"source_artifact":authority["source_artifact"],
               "membership_hash":hashlib.sha256(json.dumps(rows,sort_keys=True,default=str).encode()).hexdigest(),
               **timing, **run, "rows":rows}
    write_immutable(target,payload)
    return {"ok":True,"path":str(target),"counts":{x:sum(r["experiment"]==x for r in rows) for x in EXPERIMENTS},"broker_calls":0}

def status(dates: int) -> str:
    return "COLLECTING" if dates < 15 else "EARLY_EVIDENCE" if dates < 30 else "PRELIMINARY" if dates < 60 else "REVIEWABLE"

def values(rows: list[dict[str, Any]], h: int, field: str) -> list[float]:
    return [r[f"d{h}"][field] for r in rows if isinstance(r.get(f"d{h}"),dict) and r[f"d{h}"].get("state")=="AVAILABLE" and isinstance(r[f"d{h}"].get(field),(int,float))]

def update() -> dict[str, Any]:
    inputs = []
    for path in sorted(RESEARCH_ROOT.glob("*/*/improvement_lab_membership.json")):
        for row in (read_json(path,{}) or {}).get("rows",[]): inputs.append((path,row))
    inputs, duplicates = independent_membership_rows(inputs, ("experiment","cohort","trading_date","symbol"))
    universe = {row["symbol"] for _,row in inputs}|{"SPY"}|set(SECTOR_ETFS.values())
    resolver = ResearchPriceResolver(universe) if universe else None
    rows = [{**row,"membership_artifact":str(path),**label(row,resolver)} for path,row in inputs] if resolver else []
    rows, versioning = version_resolved_outcomes(rows,"continuous_improvement_lab",utc_now().isoformat())
    now=utc_now(); stamp=now.strftime("%Y%m%dT%H%M%SZ")
    outcomes=write_immutable(LAB/"outcomes"/f"improvement_lab_outcomes_v2_{stamp}.json",
        {"schema_version":2,"namespace":"outcomes_v2","research_only":True,"non_trading":True,"created_at":now.isoformat(),"duplicates":duplicates,"outcome_versioning":versioning,"rows":rows})
    report=[]
    for experiment in EXPERIMENTS:
        groups=defaultdict(list)
        for row in rows:
            if row["experiment"]==experiment: groups[row["cohort"]].append(row)
        for cohort, group in sorted(groups.items()):
            dates=len({r["trading_date"] for r in group})
            item={"item":experiment,"cohort":cohort,"status":status(dates),"prospective_rows":len(group),"independent_dates":dates,"mature_primary_horizon_dates":len({r["trading_date"] for r in group if (r.get("d5") or {}).get("state")=="AVAILABLE"}),"execution_type":"NEXT_OPEN_RESEARCH","data_quality_exclusions":sum(r.get("outcome_state")=="MISSING_PRICE" for r in group)}
            for h in HORIZONS:
                raw=values(group,h,"raw_return_pct"); spy=values(group,h,"spy_adjusted_return_pct")
                item[f"d{h}_mean"]=statistics.fmean(raw) if raw else None; item[f"d{h}_median"]=statistics.median(raw) if raw else None
                item[f"d{h}_win_rate"]=sum(x>0 for x in raw)/len(raw) if raw else None; item[f"d{h}_spy_adjusted"]=statistics.fmean(spy) if spy else None
            report.append(item)
        controls={"STAGE2_OFF_CONTROL_V1":("STAGE2_SELECTED","STAGE1_ALL"),"CLUSTER_OFF_CONTROL_V1":("CLUSTER_KEPT","MATCHED_STAGE2_CONTROL"),"FINALIST_OFF_CONTROL_V1":("FINALISTS","ALL_PRE_FINALIST_ELIGIBLE")}
        a,b=controls[experiment]; left=next((x for x in report if x["item"]==experiment and x["cohort"]==a),{}); right=next((x for x in report if x["item"]==experiment and x["cohort"]==b),{})
        if left and right: left["control_delta_d5_spy_adjusted"]=(left.get("d5_spy_adjusted")-right.get("d5_spy_adjusted")) if left.get("d5_spy_adjusted") is not None and right.get("d5_spy_adjusted") is not None else None
    features=[]
    stage2_rows=[r for r in rows if r["experiment"]=="STAGE2_OFF_CONTROL_V1" and r["cohort"]=="STAGE2_SELECTED"]
    for field in ("atr5MinPct","positive_extension_pct","rsVsSpy","sectorAlpha"):
        score_rows=[{**r,"final_score":(r.get("feature_snapshot") or {}).get(field)} for r in stage2_rows]
        dates=len({r["trading_date"] for r in score_rows if (r.get("d5") or {}).get("state")=="AVAILABLE"})
        ic=spearman(score_rows,5); classification="INSUFFICIENT_DATA" if dates<15 else "POSITIVE_EVIDENCE" if ic and ic>0 else "NEGATIVE_EVIDENCE" if ic and ic<0 else "NEUTRAL"
        features.append({"feature":field,"horizon":"d5","spearman_ic":ic,"independent_dates":dates,"classification":classification})
    scoreboard=write_immutable(LAB/"scoreboards"/f"improvement_lab_scoreboard_{stamp}.json",
        {"schema_version":1,"research_only":True,"non_trading":True,"created_at":now.isoformat(),"comparisons":report,"stage2_feature_scorecard":features,"minimum_dates":30,"preferred_dates":60})
    return {"ok":True,"outcomes":str(outcomes),"scoreboard":str(scoreboard),"rows":len(rows),"broker_calls":0}

def weekly() -> dict[str, Any]:
    scores=sorted((LAB/"scoreboards").glob("improvement_lab_scoreboard_*.json"))
    latest=read_json(scores[-1],{}) if scores else {}
    rows=(latest or {}).get("comparisons",[])
    queue=[]
    for name in ("STAGE1_REVIEW","STAGE2_OFF_CONTROL","STAGE2_SIMPLIFIED_V2","CLUSTER_OFF_CONTROL","FINALIST_OFF_CONTROL","PEAD_ENTRY","PEAD_EXIT_V2","PMF_EARLY_ENTRY_V2","MOMENTUM_CONTROL","CATALYST_CONTINUATION","IDIOSYNCRATIC_REVERSAL"):
        match=next((x for x in rows if x.get("item","").replace("_V1","")==name),None)
        queue.append({"ITEM":name,"TYPE":"ALPHA","CURRENT_PRODUCTION_STATE":"UNCHANGED","CHALLENGER":match.get("cohort") if match else "EXISTING_FROZEN_SHADOW_OR_PENDING_CONTROL","STATUS":match.get("status") if match else "COLLECTING","INDEPENDENT_DATES":match.get("independent_dates") if match else 0,"MATURE_OBSERVATIONS":match.get("mature_primary_horizon_dates") if match else 0,"PRIMARY_RESULT":match.get("d5_spy_adjusted") if match else None,"CONTROL_DELTA":match.get("control_delta_d5_spy_adjusted") if match else None,"NEXT_CHECKPOINT":"30 independent mature dates","RECOMMENDED_ACTION":"KEEP_COLLECTING"})
    payload={"title":"SYSTEM2 WEEKLY ALPHA REVIEW","created_at":utc_now().isoformat(),"research_only":True,"new_evidence_matured":"DESCRIPTIVE_ONLY","top_three_research_priorities":["STAGE2_OFF_CONTROL","CLUSTER_OFF_CONTROL","FINALIST_OFF_CONTROL"],"implementation_queue":queue,"infrastructure_queue":["TEST_HARNESS_ISOLATION","TRADE_ID_SEQUENCE","DISK_RETENTION","PERSISTENCE_V2_IF_NEEDED","CREDENTIAL_ROTATION"],"no_automatic_production_deployment":True}
    path=write_immutable(LAB/"weekly"/("SYSTEM2_IMPLEMENTATION_QUEUE_"+utc_now().strftime("%Y%m%dT%H%M%SZ")+".json"),payload)
    return {"ok":True,"report":str(path),"broker_calls":0}

def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument("command",choices=("create","update","weekly","self-test")); a=p.parse_args()
    result={"create":create,"update":update,"weekly":weekly,"self-test":lambda:{"ok":True,"experiments":EXPERIMENTS,"broker_calls":0,"production_changes":0}}[a.command]()
    print(json.dumps(result,sort_keys=True,default=str))
if __name__=="__main__": main()
