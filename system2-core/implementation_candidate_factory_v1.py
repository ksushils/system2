#!/usr/bin/env python3
"""Immutable research-to-shadow candidate manifests; never activates trading."""
from __future__ import annotations
import argparse, json
from typing import Any
from research_telemetry_common import RESEARCH_ROOT, read_json, utc_now, write_immutable

ROOT=RESEARCH_ROOT/"implementation_candidate_factory_v1"; REGISTRY=ROOT/"SYSTEM2_IMPLEMENTATION_CANDIDATES_V1.json"
STATUSES={"COLLECTING","EARLY_EVIDENCE","PRELIMINARY","REVIEWABLE","SHADOW_V2_READY","SHADOW_V2_ACTIVE","EXECUTION_VALIDATED","READY_FOR_OWNER_REVIEW","PRODUCTION_APPROVED","REJECTED","NOT_READY","TOO_EARLY","DESIGN_READY_WAITING_FOR_EVIDENCE"}
def candidate(cid, component, hypothesis, challenger, evidence, primary, control, requirements, risks, status="COLLECTING"):
 return {"candidate_id":cid,"candidate_version":"V1","production_component_affected":component,"hypothesis":hypothesis,"current_production_behavior":"UNCHANGED","challenger_behavior":challenger,"evidence_source":evidence,"primary_metric":primary,"secondary_metrics":["mean","median","win_rate","SPY_adjusted","sector_adjusted","coverage"],"matched_control":control,"minimum_evidence_gate":"15 independent mature dates","shadow_activation_gate":"30+ dates; positive mean/median/control delta; stable and no data-quality bias","production_review_gate":"60+ dates plus execution, cost, drawdown, capacity and regime review","execution_requirements":"NEXT_OPEN or explicitly stated executable model; no broker order","data_quality_requirements":"immutable membership, canonical XNYS prices, authoritative session, no future leakage","known_risks":risks,"status":status,"creation_timestamp":utc_now().isoformat(),"variant_count":1,"parameter_count":0}
def definitions():
 return [
 candidate("STAGE1_REDESIGN","STAGE1","Stage1 may not improve the universe or a simpler point-in-time subset may improve matched outcomes.","SIMPLER_STAGE1_V2_INTERFACE_EMPTY","STAGE1_NEXT_OPEN_BASELINE_V1_1","+5D SPY-adjusted","CURRENT_STAGE1","Future rules use existing Stage1 fields only; no thresholds defined today.","Selection/data-quality overlap", "DESIGN_READY_WAITING_FOR_EVIDENCE"),
 candidate("STAGE2_OFF","STAGE2","Skipping Stage2 may outperform current selected names.","STAGE2_OFF_SHADOW_V1: exact Stage1 population bypasses Stage2 research filter.","STAGE2_OFF_CONTROL_V1","+5D STAGE2_OFF minus CURRENT_STAGE2_SELECTED","STAGE1_ALL","Identical next-open/cost/outcome rules.","Counterfactual differs from production routing"),
 candidate("STAGE2_SIMPLIFIED_V2","STAGE2","Only prospectively validated incremental Stage2 features may improve ranking.","EMPTY_FEATURE_MANIFEST; equal-sign ranks only after admission.","FULL_STAGE2_QUARTILES_V1 + feature scorecard","+5D score IC / quintile spread","STAGE2_OFF and CURRENT_STAGE2","Feature needs positive pooled IC, non-negative median daily IC, spread, stability, conditional incremental value.","Feature redundancy / multiple testing"),
 candidate("CLUSTER_OFF","CLUSTER","Cluster filter may not add incremental alpha.","CLUSTER_OFF_SHADOW_V1: same Stage2 population with no cluster filter.","CLUSTER_OFF_CONTROL_V1","+5D kept minus matched Stage2 control","CLUSTER_KEPT / MATCHED_STAGE2_CONTROL","Immutable pair map, seed and matched liquidity/cap/sector.","Matching and concentration"),
 candidate("FINALIST_OFF","FINALIST","Finalist filter may not add incremental alpha.","FINALIST_OFF_SHADOW_V1: pre-finalist population retains identical research treatment.","FINALIST_OFF_CONTROL_V1","+5D finalists minus pre-finalist control","ALL_PRE_FINALIST_ELIGIBLE","Identical next-open and cost basis.","Overlap with cluster"),
 candidate("FINALIST_SIMPLIFIED","FINALIST","Only finalist components with prospective incremental value should survive.","EMPTY_FINALIST_FEATURE_MANIFEST","FINALIST_OFF_CONTROL_V1","+5D matched-control delta","FINALISTS / PRE_FINALIST","No rule or threshold exists until feature evidence matures.","Small cohort / selection mining","DESIGN_READY_WAITING_FOR_EVIDENCE"),
 candidate("PEAD_EXIT_V2","PEAD_EXIT","A positive PEAD entry may be impaired by current exits.","CURRENT_EXIT, FIXED_HOLD, TP1_BREAKEVEN, ATR_TIME_EXIT_V1 design-only.","PEAD_NEXT_OPEN_FIXED_HOLD_V1 + PEAD_TP1_BREAKEVEN_SHADOW_V1","matched resolved R","same PEAD event exits","40 events / 30 reaction dates; sequence certainty retained.","Intrabar ordering / exit optimization","NOT_READY"),
 candidate("PMF_V2","PMF_RETIRED","Executable earlier PMF entry may retain modeled edge; PMF V1 remains retired.","PMF_V2_SHADOW using MODEL_REFERENCE, NEXT_OPEN, FIRST_PULLBACK_V1, PMF_V1_CONFIRMATION only.","PMF_EARLY_ENTRY_SHADOW_V1","+3D/+5D executable matched evidence","MODEL_REFERENCE diagnostic upper bound","NEXT_OPEN or FIRST_PULLBACK must be positive prospectively.","Squeeze/extension and modeled-execution gap"),
 candidate("MOMENTUM","NONE","Frozen MOM_6_1/12_1 may provide independent alpha.","MOMENTUM_SHADOW_V2_READY_ONLY","MOMENTUM_CONTROL_V1","20D SPY-adjusted","matched Stage1 control","Turnover, sector, overlap with PEAD/System2 required.","Momentum duplication"),
 candidate("CATALYST_CONTINUATION","NONE","Frozen catalyst continuation may be executable before complexity is added.","CATALYST_EXECUTION_SHADOW_V2_READY_ONLY","CATALYST_CONTINUATION_V1","5D SPY-adjusted","matched control","Analyze timestamp, delay, gap, liquidity; no category optimization.","Event data timing"),
 candidate("IDIOSYNCRATIC_REVERSAL","NONE","Reversal can be independent of beta only if catalyst states stay separate.","REVERSAL_SHADOW_V2_READY_ONLY","IDIOSYNCRATIC_REVERSAL_V1","3D direction-adjusted","matched control","Keep NO_MAJOR_CATALYST and KNOWN_CATALYST separate.","Market/sector beta"),
 candidate("SYSTEM2_5_V4","SYSTEM_ASSEMBLY","Only individually incremental modules can form a complementary system.","ASSEMBLY_INTERFACE_EMPTY","all approved candidate scorecards","incremental portfolio/module delta","module overlap analysis","Modules must be REVIEWABLE individually; assess symbol/date/return/sector/factor overlap.","Combination overfit","TOO_EARLY")]
def ensure():
 x=read_json(REGISTRY,None)
 if isinstance(x,dict): return x
 rows=definitions(); payload={"schema_version":1,"registry_name":"SYSTEM2_IMPLEMENTATION_CANDIDATES_V1","research_only":True,"non_trading":True,"creation_timestamp":utc_now().isoformat(),"promotion_rule":"No script may set PRODUCTION_APPROVED; explicit owner decision required.","candidates":rows,"rejected_candidates":[]}
 write_immutable(REGISTRY,payload)
 for row in rows:
  write_immutable(ROOT/"manifests"/f"{row['candidate_id']}_V1.json",{"schema_version":1,"manifest_type":"READY_TO_LAUNCH_SHADOW_DESIGN","research_only":True,"non_trading":True,"inputs":row["evidence_source"],"outputs":["immutable membership","canonical outcomes","matched-control report"],"entry_model":"NEXT_OPEN unless manifest says otherwise","exit_model":"research outcome horizon; PEAD only has predeclared exits","risk_model":"NONE — research only","data_sources":"point-in-time authoritative artifacts + Research Measurement V2","required_evidence_gate":row["shadow_activation_gate"],"candidate":row})
 return read_json(REGISTRY,{})
def report():
 x=ensure(); rows=[]
 for c in x["candidates"]:
  status=c["status"]; action="KEEP_COLLECTING"
  if status in {"NOT_READY","TOO_EARLY","DESIGN_READY_WAITING_FOR_EVIDENCE"}: action="KEEP_COLLECTING"
  rows.append({"ITEM":c["candidate_id"],"CURRENT_PRODUCTION_COMPONENT":c["production_component_affected"],"CURRENT_TEST":c["evidence_source"],"CANDIDATE_SHADOW":c["challenger_behavior"],"CURRENT_STATUS":status,"MATURE_DATES":0,"PRIMARY_RESULT":None,"CONTROL_DELTA":None,"SHADOW_GATE":c["shadow_activation_gate"],"MISSING_EVIDENCE":"mature prospective outcomes","NEXT_ACTION":action})
 return write_immutable(ROOT/"reports"/("SYSTEM2_IMPLEMENTATION_CANDIDATE_REPORT_"+utc_now().strftime("%Y%m%dT%H%M%SZ")+".json"),{"title":"SYSTEM2_IMPLEMENTATION_CANDIDATE_REPORT","research_only":True,"non_trading":True,"created_at":utc_now().isoformat(),"gate_detection":{"15_date":"not crossed","30_date":"not crossed","60_date":"not crossed","pead_event":"not crossed","pmf_signal":"not crossed"},"priority_engine":"Impact, maturity, control quality, data quality, complexity, execution realism — not return alone.","rows":rows})
def main():
 p=argparse.ArgumentParser();p.add_argument("command",choices=("init","report","self-test"));a=p.parse_args(); x=ensure()
 if a.command=="report": out={"report":str(report()),"broker_calls":0,"production_changes":0}
 elif a.command=="self-test": out={"ok":all(c["status"] in STATUSES for c in x["candidates"]),"candidates":len(x["candidates"]),"broker_calls":0,"production_changes":0}
 else: out={"registry":str(REGISTRY),"broker_calls":0,"production_changes":0}
 print(json.dumps(out))
if __name__=="__main__": main()
