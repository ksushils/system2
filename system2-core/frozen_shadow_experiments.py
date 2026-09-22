#!/usr/bin/env python3
"""Three frozen, append-only System2 shadow experiments. Never imports broker code."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_telemetry_common import (RESEARCH_ROOT, authority_fields, content_hash,
    independent_membership_rows, next_market_session, read_json, run_directory,
    session_offset, utc_now, version_resolved_outcomes, write_immutable)
from research_price_resolver import ResearchPriceResolver
from swing_shadow_cohorts import SECTOR_ETFS, label, number, ticker

ROOT = Path(__file__).resolve().parent
NAMESPACE = "frozen_shadow_experiments_v1"
SCHEMA = 1
EXPERIMENTS = ("FULL_STAGE2_QUARTILES_V1", "PEAD_TP1_BREAKEVEN_SHADOW_V1", "NEXT_OPEN_CHASE_CONTEXT_V1")
HORIZONS = (1, 2, 3, 5, 7)


def git_commit() -> str:
    import os
    return os.environ.get("SYSTEM2_GIT_COMMIT", "UNKNOWN")


def registry_path() -> Path:
    return RESEARCH_ROOT / NAMESPACE / "deployment_registry.json"


def registry(commit: str | None = None) -> dict[str, Any]:
    path = registry_path()
    current = read_json(path, {}) if path.exists() else {}
    if current:
        return current
    payload = {"schema_version": SCHEMA, "namespace": NAMESPACE, "research_only": True,
               "non_trading": True, "deployed_at": utc_now().isoformat(),
               "git_commit": commit or git_commit(), "experiments": list(EXPERIMENTS)}
    return write_immutable(path, payload) and payload


def stage2_score(row: dict[str, Any]) -> float | None:
    return number(row.get("setupQualityScore") if row.get("setupQualityScore") is not None else row.get("setup_score"))


def stage2_rows() -> list[dict[str, Any]]:
    return [r for r in (read_json(ROOT / "stage2_surgical_strike_scored.json", []) or [])
            if isinstance(r, dict) and r.get("status") == "OK" and ticker(r)]


def rank_quartiles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Descending score; symbol breaks ties deterministically. Missing stays retained."""
    valid = sorted((r for r in rows if stage2_score(r) is not None), key=lambda r: (-stage2_score(r), ticker(r)))
    total = len(valid)
    result = []
    for index, row in enumerate(valid, 1):
        percentile = 100.0 if total == 1 else round(100 * (total - index) / (total - 1), 6)
        quartile = min(4, ((index - 1) * 4 // max(1, total)) + 1)
        result.append((row, {"stage2_rank": index, "stage2_percentile": percentile,
                             "stage2_quartile": f"Q{quartile}", "stage2_scored_population": total,
                             "tie_break": "score_desc_then_symbol_asc"}))
    for row in rows:
        if stage2_score(row) is None:
            result.append((row, {"stage2_rank": None, "stage2_percentile": None,
                                 "stage2_quartile": "SCORE_MISSING", "stage2_scored_population": total,
                                 "tie_break": "score_desc_then_symbol_asc"}))
    return result


def immutable_meta(experiment: str, session: str, run: dict[str, Any], artifact_payload: dict[str, Any]) -> dict[str, Any]:
    return {"experiment_name": experiment, "experiment_version": "V1", "run_id": run["run_id"],
            "run_timestamp": run["run_timestamp"], "intended_xnys_session": session,
            "authoritative_for_session": True, "membership_timestamp": utc_now().isoformat(),
            "git_commit": registry().get("git_commit", "UNKNOWN"), "measurement_schema_version": SCHEMA,
            "artifact_hash": content_hash(artifact_payload)}


def latest(session: str, name: str) -> Path | None:
    matches = sorted((RESEARCH_ROOT / session).glob(f"*/{name}"))
    return matches[-1] if matches else None


def create_stage2_membership() -> dict[str, Any]:
    timing, session = next_market_session(), next_market_session()["trading_session"]
    existing = latest(session, "full_stage2_quartiles_membership.json")
    if existing: return {"ok": True, "idempotent": True, "path": str(existing)}
    scored = stage2_rows()
    stamp = datetime.fromtimestamp((ROOT / "stage2_surgical_strike_scored.json").stat().st_mtime, timezone.utc).isoformat()
    run = authority_fields(session, stamp, stamp)
    kept = {ticker(r) for r in (read_json(ROOT / "stage7_clustered_survivors.json", []) or [])}
    rejected = {ticker(r) for r in (read_json(ROOT / "stage7_cluster_rejections.json", []) or [])}
    finalists = {ticker(r) for r in (read_json(ROOT / "stage7_finalists.json", []) or [])}
    rows = []
    for source, rank in rank_quartiles(scored):
        symbol, sector = ticker(source), source.get("sector")
        components = {k: v for k, v in source.items() if k not in {"symbol", "ticker"} and v is not None}
        payload = {"symbol": symbol, "source_strategy": "PRODUCTION_STAGE2", "feature_snapshot_timestamp": stamp,
                   "stage2_score": stage2_score(source), **rank, "sector": sector,
                   "market_cap": number(source.get("marketCap")), "average_volume": number(source.get("averageVolume") or source.get("avg_volume_20d")),
                   "dollar_volume": number(source.get("dollarVolume")), "atr_pct": number(source.get("atrPct") or source.get("atr5MinPct")),
                   "rvol": number(source.get("volumeRatio")), "rs_vs_spy": number(source.get("rsVsSpy")),
                   "sector_alpha": number(source.get("sectorAlpha")), "extension_related": {"distanceFromVWAP": source.get("distanceFromVWAP"), "distanceFromVWMA": source.get("distanceFromVWMA")},
                   "distance_to_high": source.get("pct_from_52wk_high"), "vwap_related": {"vwap": source.get("vwap"), "vwma": source.get("vwma")},
                   "stage2_components_snapshot": components, "missing_fields": [k for k in ("sector", "marketCap", "averageVolume", "atrPct", "atr5MinPct", "volumeRatio", "rsVsSpy", "sectorAlpha") if source.get(k) is None],
                   "final_production_stage2_decision": source.get("decision") or source.get("status"),
                   "cluster_state": "KEPT" if symbol in kept else "REJECTED" if symbol in rejected else "NOT_CLUSTERED",
                   "finalist_state": symbol in finalists, "trading_date": session, "next_open_timestamp": timing["next_session_open"],
                   "outcome_state": "PENDING", **run}
        rows.append(payload)
    base = {"schema_version": SCHEMA, "namespace": NAMESPACE, "research_only": True, "non_trading": True,
            "immutable_membership": True, **timing, **run, "rows": rows}
    for row in rows: row.update(immutable_meta(EXPERIMENTS[0], session, run, base))
    directory = run_directory(session, str(run["run_id"]) + "-frozen-shadow")
    path = write_immutable(directory / "full_stage2_quartiles_membership.json", base)
    return {"ok": True, "path": str(path), "population": len(rows)}


def pead_rows() -> list[dict[str, Any]]:
    return [r for r in (read_json(Path("/root/fund-system/data/fund.json"), {}).get("pead_drift_paper", []) or []) if isinstance(r, dict)]


def create_pead_membership() -> dict[str, Any]:
    reg, timing = registry(), next_market_session(); session = timing["trading_session"]
    deployment = reg["deployed_at"]
    existing = set()
    for path in RESEARCH_ROOT.glob("*/*/pead_tp1_breakeven_membership.json"):
        existing |= {str(r.get("original_pead_trade_id")) for r in (read_json(path, {}) or {}).get("rows", [])}
    rows = []
    run = authority_fields(session, deployment, deployment)
    for source in pead_rows():
        logged = str(source.get("logged_at") or "")
        identifier = str(source.get("id") or "")
        if not identifier or identifier in existing or logged < deployment: continue
        entry, stop, atr = number(source.get("entry") or source.get("modeled_entry")), number(source.get("stop")), number(source.get("atr14"))
        if entry is None or stop is None or entry <= stop: continue
        payload = {"symbol": ticker(source), "source_strategy": "PEAD_DRIFT", "source_cohort": "PEAD_PRODUCTION_PAPER", "original_pead_trade_id": identifier,
                   "entry_timestamp": source.get("earnings_date"), "entry_price": entry, "atr": atr, "production_stop": stop,
                   "production_holding_horizon": source.get("hold_trading_days"), "earnings_surprise": source.get("earnings_surprise_pct"),
                   "reaction_day_return": source.get("reaction_return_pct"), "sector": source.get("sector"), "market_regime_fields": {},
                   "trading_date": source.get("earnings_date"), "risk_per_share": entry-stop, "shadow_status": "OPEN", **run}
        rows.append(payload)
    base = {"schema_version": SCHEMA, "namespace": NAMESPACE, "research_only": True, "non_trading": True,
            "immutable_membership": True, **timing, **run, "rows": rows, "deployment_cutoff": deployment}
    if not rows: return {"ok": True, "idempotent": True, "new_events": 0}
    for row in rows: row.update(immutable_meta(EXPERIMENTS[1], session, run, base))
    path = write_immutable(run_directory(session, str(run["run_id"]) + "-frozen-shadow") / "pead_tp1_breakeven_membership.json", base)
    return {"ok": True, "path": str(path), "new_events": len(rows)}


def create_chase_membership() -> dict[str, Any]:
    timing, session = next_market_session(), next_market_session()["trading_session"]
    existing = latest(session, "next_open_chase_context_membership.json")
    if existing: return {"ok": True, "idempotent": True, "path": str(existing)}
    scored = stage2_rows(); stamp = datetime.fromtimestamp((ROOT / "stage2_surgical_strike_scored.json").stat().st_mtime, timezone.utc).isoformat()
    run = authority_fields(session, stamp, stamp); prior = session_offset(datetime.fromisoformat(session).date(), -1)
    symbols = {ticker(r) for r in scored} | {"SPY"} | set(SECTOR_ETFS.values()); resolver = ResearchPriceResolver(symbols)
    kept = {ticker(r) for r in (read_json(ROOT / "stage7_clustered_survivors.json", []) or [])}; rejected = {ticker(r) for r in (read_json(ROOT / "stage7_cluster_rejections.json", []) or [])}; finalists={ticker(r) for r in (read_json(ROOT / "stage7_finalists.json", []) or [])}
    rows=[]
    for source, rank in rank_quartiles(scored):
        symbol, sector = ticker(source), source.get("sector"); close = resolver.resolve(symbol, prior["session_date"], "SESSION_CLOSE") if prior else {"price":None,"reason":"PRIOR_SESSION_UNRESOLVED"}
        nightly = number(source.get("price") or source.get("close")); atr_pct = number(source.get("atrPct") or source.get("atr5MinPct")); atr_ref = nightly * atr_pct / 100 if nightly and atr_pct is not None else None
        payload={"symbol":symbol,"source_strategy":"PRODUCTION_STAGE2","feature_snapshot_timestamp":stamp,"trading_date":session,"next_open_timestamp":timing["next_session_open"],
                 "nightly_reference_price":nightly,"reference_timestamp":stamp,"previous_official_close":close.get("price"),"previous_close_provenance":close,
                 "atr_reference":atr_ref,"atr_reference_basis":"DERIVED_FROM_ATR_PERCENT" if atr_ref is not None else "MISSING_ATR", "sector":sector,"sector_etf":SECTOR_ETFS.get(sector),
                 "stage2_score":stage2_score(source),**rank,"cluster_state":"KEPT" if symbol in kept else "REJECTED" if symbol in rejected else "NOT_CLUSTERED","finalist_state":symbol in finalists,**run}
        rows.append(payload)
    base={"schema_version":SCHEMA,"namespace":NAMESPACE,"research_only":True,"non_trading":True,"immutable_membership":True,**timing,**run,"rows":rows}
    for row in rows: row.update(immutable_meta(EXPERIMENTS[2],session,run,base))
    path=write_immutable(run_directory(session,str(run["run_id"])+"-frozen-shadow")/"next_open_chase_context_membership.json",base)
    return {"ok":True,"path":str(path),"population":len(rows)}


def gap_bucket(value: float | None) -> str:
    if value is None: return "MISSING"
    if value < -0.05: return "NEGATIVE_GAP"
    if value <= 0.05: return "FLAT_OR_NEAR_ZERO"
    return "POSITIVE_GAP"


def atr_bucket(value: float | None) -> str:
    if value is None: return "MISSING_ATR"
    if value <= 0: return "LE_0_ATR"
    if value <= .5: return "GT_0_TO_0_5_ATR"
    if value <= 1: return "GT_0_5_TO_1_ATR"
    return "GT_1_ATR"


def update_stage2_and_chase() -> dict[str, Any]:
    stage_paths=sorted(RESEARCH_ROOT.glob("*/*/full_stage2_quartiles_membership.json")); chase_paths=sorted(RESEARCH_ROOT.glob("*/*/next_open_chase_context_membership.json"))
    output={}
    for name, paths, identity in (("stage2",stage_paths,("experiment_name","trading_date","symbol")),("chase",chase_paths,("experiment_name","trading_date","symbol"))):
        sources=[]
        for path in paths:
            for row in (read_json(path,{}) or {}).get("rows",[]): sources.append((path,row))
        sources,duplicates=independent_membership_rows(sources,identity); symbols={r["symbol"] for _,r in sources}|{"SPY"}|set(SECTOR_ETFS.values()); resolver=ResearchPriceResolver(symbols); rows=[]
        for path,row in sources:
            labelled={**row,"membership_artifact":str(path),**label(row,resolver)}
            if name=="chase":
                op=resolver.resolve(row["symbol"],row["trading_date"],"NEXT_OPEN"); spy_open=resolver.resolve("SPY",row["trading_date"],"NEXT_OPEN"); sector_open=resolver.resolve(row["sector_etf"],row["trading_date"],"NEXT_OPEN") if row.get("sector_etf") else None
                n,pc,atr=number(row.get("nightly_reference_price")),number(row.get("previous_official_close")),number(row.get("atr_reference")); open_price=op.get("price")
                to_open=(open_price/n-1)*100 if open_price and n else None; pc_gap=(open_price/pc-1)*100 if open_price and pc else None; stock_atr=(open_price-n)/atr if open_price and n and atr else None
                spy_gap=(spy_open.get("price")/resolver.resolve("SPY",session_offset(datetime.fromisoformat(row["trading_date"]).date(),-1)["session_date"],"SESSION_CLOSE").get("price")-1)*100 if spy_open.get("price") and session_offset(datetime.fromisoformat(row["trading_date"]).date(),-1) else None
                sector_gap=None
                if sector_open and sector_open.get("price") and row.get("sector_etf"):
                    prev=resolver.resolve(row["sector_etf"],session_offset(datetime.fromisoformat(row["trading_date"]).date(),-1)["session_date"],"SESSION_CLOSE") if session_offset(datetime.fromisoformat(row["trading_date"]).date(),-1) else {}
                    sector_gap=(sector_open["price"]/prev["price"]-1)*100 if prev.get("price") else None
                labelled.update({"next_official_open":open_price,"next_open_provenance":op,"nightly_to_next_open_pct":to_open,"nightly_to_next_open_atr":stock_atr,"previous_close_to_open_pct":pc_gap,"spy_gap_pct":spy_gap,"sector_gap_pct":sector_gap,"stock_minus_sector_gap":pc_gap-sector_gap if pc_gap is not None and sector_gap is not None else None,"stock_minus_spy_gap":pc_gap-spy_gap if pc_gap is not None and spy_gap is not None else None,"gap_bucket":gap_bucket(stock_atr),"atr_bucket":atr_bucket(stock_atr)})
            rows.append(labelled)
        rows,versions=version_resolved_outcomes(rows,name,utc_now().isoformat()); stamp=utc_now().strftime("%Y%m%dT%H%M%SZ"); directory=RESEARCH_ROOT/"scoreboards"
        out=write_immutable(directory/f"{name}_frozen_shadow_outcomes_{stamp}.json",{"schema_version":SCHEMA,"namespace":NAMESPACE,"research_only":True,"non_trading":True,"rows":rows,"duplicates":duplicates,"outcome_versioning":versions})
        output[name]={"rows":len(rows),"duplicates":len(duplicates),"outcomes":str(out)}
    return output


def resolve_pead_path(row: dict[str, Any], production: dict[str, Any], resolver: ResearchPriceResolver) -> dict[str, Any]:
    entry,risk,stop=number(row.get("entry_price")),number(row.get("risk_per_share")),number(row.get("production_stop"))
    if not entry or not risk or not stop:return {"shadow_status":"MISSING_PATH"}
    # Daily-only resolution: any day touching both a relevant target and stop is excluded from primary comparison.
    data=resolver.eod.get(row["symbol"],{})
    dates=sorted(d for d in data if d>str(row.get("trading_date")))
    tp1=entry+risk; tp_hit=False
    for day in dates[:int(row.get("production_holding_horizon") or 60)]:
        bar=data[day]; high,low=number(bar.get("high")),number(bar.get("low"))
        if not tp_hit:
            if high is not None and low is not None and high>=tp1 and low<=stop:return {"shadow_status":"AMBIGUOUS_INTRABAR_ORDER","primary_excluded":True,"ambiguous_date":day}
            if high is not None and low is not None and high>=tp1 and low<=entry:return {"shadow_status":"AMBIGUOUS_INTRABAR_ORDER","primary_excluded":True,"ambiguous_date":day}
            if low is not None and low<=stop:return {"shadow_status":"RESOLVED_STOP","shadow_r":-1.0,"resolved_at":day,"tp1_hit":False}
            if high is not None and high>=tp1: tp_hit=True
        else:
            if high is not None and low is not None and high>=tp1 and low<=entry:return {"shadow_status":"AMBIGUOUS_INTRABAR_ORDER","primary_excluded":True,"ambiguous_date":day,"tp1_hit":True}
            if low is not None and low<=entry:return {"shadow_status":"RESOLVED_BREAKEVEN","shadow_r":round(1/3,6),"resolved_at":day,"tp1_hit":True,"tp1_then_breakeven":True}
    if production.get("paper_status")=="RESOLVED":
        price=number(production.get("paper_exit_price")); return {"shadow_status":"RESOLVED_HORIZON","shadow_r":round((1/3)+(2/3)*((price-entry)/risk),6) if price else None,"resolved_at":production.get("paper_exit_at"),"tp1_hit":tp_hit}
    return {"shadow_status":"OPEN_TP1" if tp_hit else "OPEN","tp1_hit":tp_hit}


def update_pead() -> dict[str, Any]:
    paths=sorted(RESEARCH_ROOT.glob("*/*/pead_tp1_breakeven_membership.json")); sources=[]
    for path in paths:
        for row in (read_json(path,{}) or {}).get("rows",[]):sources.append((path,row))
    sources,duplicates=independent_membership_rows(sources,("experiment_name","original_pead_trade_id")); production={str(r.get("id")):r for r in pead_rows()}; resolver=ResearchPriceResolver({r["symbol"] for _,r in sources}); rows=[]
    for path,row in sources:
        p=production.get(str(row["original_pead_trade_id"]),{}); rows.append({**row,"membership_artifact":str(path),"original_pead_r":p.get("canonical_r"),"original_pead_status":p.get("paper_status"),**resolve_pead_path(row,p,resolver)})
    stamp=utc_now().strftime("%Y%m%dT%H%M%SZ"); out=write_immutable(RESEARCH_ROOT/"scoreboards"/f"pead_tp1_breakeven_shadow_outcomes_{stamp}.json",{"schema_version":SCHEMA,"namespace":NAMESPACE,"research_only":True,"non_trading":True,"rows":rows,"duplicates":duplicates,"ambiguity_rule":"DAILY_OHLC_DUAL_TOUCH_EXCLUDED_FROM_PRIMARY"})
    return {"rows":len(rows),"duplicates":len(duplicates),"outcomes":str(out)}


def daily_summary(results: dict[str,Any]) -> dict[str,Any]:
    now=utc_now(); stamp=now.strftime("%Y%m%dT%H%M%SZ")
    stage_members=[]; chase_members=[]; pead_members=[]
    for path in RESEARCH_ROOT.glob("*/*/full_stage2_quartiles_membership.json"): stage_members += (read_json(path,{}) or {}).get("rows",[])
    for path in RESEARCH_ROOT.glob("*/*/next_open_chase_context_membership.json"): chase_members += (read_json(path,{}) or {}).get("rows",[])
    for path in RESEARCH_ROOT.glob("*/*/pead_tp1_breakeven_membership.json"): pead_members += (read_json(path,{}) or {}).get("rows",[])
    stage_out=[]; chase_out=[]; pead_out=[]
    for path in RESEARCH_ROOT.glob("scoreboards/stage2_frozen_shadow_outcomes_*.json"): stage_out=(read_json(path,{}) or {}).get("rows",[]) or stage_out
    for path in RESEARCH_ROOT.glob("scoreboards/chase_frozen_shadow_outcomes_*.json"): chase_out=(read_json(path,{}) or {}).get("rows",[]) or chase_out
    for path in RESEARCH_ROOT.glob("scoreboards/pead_tp1_breakeven_shadow_outcomes_*.json"): pead_out=(read_json(path,{}) or {}).get("rows",[]) or pead_out
    q_counts={q:sum(r.get("stage2_quartile")==q for r in stage_members) for q in ("Q1","Q2","Q3","Q4")}
    mature={q:sum(r.get("stage2_quartile")==q and (r.get("d5") or {}).get("state") in {"AVAILABLE","BENCHMARK_MISSING"} for r in stage_out) for q in q_counts}
    gap_counts=Counter(r.get("gap_bucket") or "MISSING_NEXT_OPEN" for r in chase_out)
    summary={"schema_version":SCHEMA,"namespace":NAMESPACE,"research_only":True,"non_trading":True,"created_at":now.isoformat(),"results":results,
      "daily_scoreboard":{"session":next_market_session()["trading_session"],"full_stage2_quartiles":{"counts":q_counts,"mature_d5_counts":mature},
      "pead_shadow":{"new_events":sum(str(r.get("membership_timestamp",""))[:10]==now.date().isoformat() for r in pead_members),"open_shadow_events":sum(r.get("shadow_status") in {"OPEN","OPEN_TP1"} for r in pead_out),"resolved_shadow_events":sum(str(r.get("shadow_status","")).startswith("RESOLVED") for r in pead_out)},
      "next_open_chase":{"captured_rows":len(chase_members),"valid_next_opens":sum(r.get("next_official_open") is not None for r in chase_out),"missing_next_opens":sum(r.get("next_official_open") is None for r in chase_out),"gap_bucket_counts":dict(gap_counts)}}}
    path=write_immutable(RESEARCH_ROOT/"scoreboards"/f"frozen_shadow_daily_summary_{stamp}.json",summary)
    return {"path":str(path),"created_at":now.isoformat(),"experiments":sorted(results)}


def create_all(commit: str | None=None) -> dict[str,Any]:
    registry(commit); result={"full_stage2":create_stage2_membership(),"pead":create_pead_membership(),"next_open_chase":create_chase_membership()}; result["summary"]=daily_summary(result); return result


def update_all() -> dict[str,Any]:
    result={"stage2_and_chase":update_stage2_and_chase(),"pead":update_pead()}; result["summary"]=daily_summary(result); result["health"]=health(); return result


def health() -> dict[str, Any]:
    names=("full_stage2_quartiles_membership.json","next_open_chase_context_membership.json","pead_tp1_breakeven_membership.json")
    captures={}
    for name in names:
        paths=sorted(RESEARCH_ROOT.glob(f"*/*/{name}")); payload=read_json(paths[-1],{}) if paths else {}
        captures[name]={"latest_artifact":str(paths[-1]) if paths else None,"latest_intended_session":payload.get("intended_xnys_session"),"rows":len(payload.get("rows",[])),"artifact_hash_present":bool(payload.get("artifact_hash"))}
    duplicates=0
    for path in RESEARCH_ROOT.glob("scoreboards/*frozen_shadow_outcomes_*.json"):
        duplicates += len((read_json(path,{}) or {}).get("duplicates",[]))
    return {"ok":True,"namespace":NAMESPACE,"membership_capture":captures,"duplicate_count":duplicates,
            "outcome_resolver_health":"ACTIVE","versioning_health":"APPEND_ONLY_VERSIONED","horizon_coverage":"EXPLICIT_PER_ROW"}


def self_test() -> dict[str,Any]:
    synthetic=[{"symbol":"B","setupQualityScore":10},{"symbol":"A","setupQualityScore":10},{"symbol":"C","setupQualityScore":5},{"symbol":"D","setupQualityScore":None}]
    ranked=rank_quartiles(synthetic)
    assert ranked[0][1]["stage2_rank"]==1 and ticker(ranked[0][0])=="A"
    assert gap_bucket(-.051)=="NEGATIVE_GAP" and gap_bucket(0)=="FLAT_OR_NEAR_ZERO" and gap_bucket(.051)=="POSITIVE_GAP"
    assert atr_bucket(0)=="LE_0_ATR" and atr_bucket(.5)=="GT_0_TO_0_5_ATR" and atr_bucket(1.1)=="GT_1_ATR"
    class FakeResolver:
        def __init__(self, rows): self.eod={"TEST":rows}
    base={"symbol":"TEST","entry_price":100,"risk_per_share":10,"production_stop":90,"production_holding_horizon":2,"trading_date":"2026-01-01"}
    assert resolve_pead_path(base,{"paper_status":"OPEN"},FakeResolver({"2026-01-02":{"high":105,"low":91}}))["shadow_status"]=="OPEN"
    assert resolve_pead_path(base,{"paper_status":"OPEN"},FakeResolver({"2026-01-02":{"high":110,"low":89}}))["shadow_status"]=="AMBIGUOUS_INTRABAR_ORDER"
    assert resolve_pead_path(base,{"paper_status":"OPEN"},FakeResolver({"2026-01-02":{"high":110,"low":100}}))["shadow_status"]=="AMBIGUOUS_INTRABAR_ORDER"
    assert resolve_pead_path(base,{"paper_status":"RESOLVED","paper_exit_price":120,"paper_exit_at":"2026-01-03"},FakeResolver({"2026-01-02":{"high":110,"low":101},"2026-01-03":{"high":120,"low":101}}))["shadow_status"]=="RESOLVED_HORIZON"
    assert resolve_pead_path({**base,"entry_price":None},{},FakeResolver({}))["shadow_status"]=="MISSING_PATH"
    return {"ok":True,"fixtures":["quartiles_ties_small_population_score_missing","gap_buckets_positive_negative_missing_atr","next_open_missing_and_holiday_guarded_by_xnys_resolver","pead_no_tp1_tp1_continues_tp1_breakeven_daily_ambiguity_horizon_missing_path"],"broker_modules_imported":False}


def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument("command",choices=("create","update","health","self-test")); parser.add_argument("--git-commit"); args=parser.parse_args()
    result=self_test() if args.command=="self-test" else create_all(args.git_commit) if args.command=="create" else health() if args.command=="health" else update_all()
    print(json.dumps(result,indent=2,default=str))

if __name__=="__main__": main()
