#!/usr/bin/env python3
"""Canonical, non-trading alpha evaluation contract and date-equal evaluator."""
from __future__ import annotations
import argparse,json,statistics
from collections import defaultdict
from pathlib import Path
from research_telemetry_common import RESEARCH_ROOT,read_json,utc_now,write_immutable
ROOT=RESEARCH_ROOT/"canonical_alpha_evaluation_v1"; CONTRACT=ROOT/"SYSTEM2_CANONICAL_ALPHA_EVALUATION_V1.json"
FIELDS=["experiment_id","candidate_id","candidate_version","membership_version","intended_session","symbol","entry_type","entry_session","entry_price","entry_source","entry_state","horizon","target_session","target_price","target_source","outcome_state","raw_return","SPY_return","SPY_adjusted_return","sector_return","sector_adjusted_return","R_return","control_id","pair_id","data_quality_state"]
def ensure():
 x=read_json(CONTRACT,None)
 if isinstance(x,dict):return x
 return read_json(write_immutable(CONTRACT,{"schema_version":1,"contract":"SYSTEM2_CANONICAL_ALPHA_EVALUATION_V1","research_only":True,"non_trading":True,"fields":FIELDS,"entry_types":["NEXT_OPEN","REAL_FILL","PAPER_FILL","MODEL_REFERENCE","FIRST_PULLBACK","CONFIRMATION_ENTRY"],"maturity_states":["PENDING","PENDING_CANONICAL_XNYS_OPEN","AWAITING_CANONICAL_PRICE","VALID","MISSING_PRICE","INVALID_SESSION","DATA_QUALITY_EXCLUSION"],"primary_weighting":"DATE_EQUAL","promotion_requires":"explicit owner review","created_at":utc_now().isoformat()}),{})
def gate(n):return "COLLECTING" if n<15 else "EARLY_EVIDENCE" if n<30 else "PRELIMINARY" if n<60 else "REVIEWABLE"
def date_equal(rows):
 by=defaultdict(list)
 for r in rows:
  if r.get("outcome_state")=="VALID" and isinstance(r.get("SPY_adjusted_return"),(int,float)):by[r["intended_session"]].append(r["SPY_adjusted_return"])
 vals=[statistics.fmean(v) for v in by.values()]
 return {"dates":len(vals),"date_equal_mean":statistics.fmean(vals) if vals else None,"pooled_mean":statistics.fmean([x for v in by.values() for x in v]) if vals else None,"median":statistics.median(vals) if vals else None}
def evaluate():
 ensure(); candidates=["STAGE1_REDESIGN","STAGE2_OFF","STAGE2_SIMPLIFIED_V2","CLUSTER_OFF","FINALIST_OFF","FINALIST_SIMPLIFIED","PEAD_EXIT_V2","PMF_V2","MOMENTUM","CATALYST_CONTINUATION","IDIOSYNCRATIC_REVERSAL","SYSTEM2_5_V4"]
 # Only authoritative corrective direct-control artifact is admitted; original V1 remains audit-only.
 memberships=sorted(RESEARCH_ROOT.glob("2026-09-25/*/improvement_lab_membership.json"))
 rows=[]; seen=set(); duplicates=0
 for p in memberships:
  for r in (read_json(p,{})or{}).get("rows",[]):
   key=(r.get("experiment"),r.get("cohort"),r.get("trading_date"),r.get("symbol"))
   if key in seen:duplicates+=1;continue
   seen.add(key); rows.append({"experiment_id":r.get("experiment"),"candidate_id":r.get("experiment"),"candidate_version":"V1","membership_version":"immutable","intended_session":r.get("trading_date"),"symbol":r.get("symbol"),"entry_type":"NEXT_OPEN","entry_session":r.get("trading_date"),"entry_price":None,"entry_source":"CANONICAL_XNYS_OPEN","entry_state":"PENDING_CANONICAL_XNYS_OPEN","horizon":"d5","target_session":None,"target_price":None,"target_source":None,"outcome_state":"PENDING","raw_return":None,"SPY_return":None,"SPY_adjusted_return":None,"sector_return":None,"sector_adjusted_return":None,"R_return":None,"control_id":r.get("cohort"),"pair_id":None,"data_quality_state":"PENDING"})
 stats=date_equal(rows); out=[]
 for c in candidates:out.append({"candidate":c,"status":"NOT_READY" if c=="PEAD_EXIT_V2" else "TOO_EARLY" if c=="SYSTEM2_5_V4" else "COLLECTING","mature_dates":stats["dates"],"rows":len(rows) if c in ("STAGE2_OFF","CLUSTER_OFF","FINALIST_OFF") else 0,"primary_metric":"+5D date-equal SPY-adjusted","primary_result":None,"control_result":None,"control_delta":None,"median":stats["median"],"coverage":0,"next_gate":15,"dates_needed":15-stats["dates"]})
 payload={"title":"SYSTEM2_CANONICAL_ALPHA_REVIEW","created_at":utc_now().isoformat(),"research_only":True,"non_trading":True,"contract_hash":read_json(CONTRACT,{ }).get("artifact_hash"),"duplicates_detected":duplicates,"duplicates_excluded":duplicates,"analytical_authority_used":"SEP25 corrective/direct immutable cohorts only","alpha_waterfall":"PENDING_NO_MATURE_OUTCOMES","rows":out,"canonical_rows":rows,"performance":{"runtime_scope":"single Sep25 authoritative membership","peak_rss":"NOT_MEASURED","output_bytes":"bounded"}}
 return write_immutable(ROOT/"reports"/("canonical_alpha_evaluation_"+utc_now().strftime("%Y%m%dT%H%M%SZ")+".json"),payload)
def test():
 assert date_equal([{"intended_session":"a","outcome_state":"VALID","SPY_adjusted_return":1},{"intended_session":"a","outcome_state":"VALID","SPY_adjusted_return":3},{"intended_session":"b","outcome_state":"VALID","SPY_adjusted_return":9}])["date_equal_mean"]==5
 assert gate(14)=="COLLECTING" and gate(15)=="EARLY_EVIDENCE"
 return {"ok":True,"broker_calls":0,"production_changes":0}
def main():
 p=argparse.ArgumentParser();p.add_argument("command",choices=("init","evaluate","self-test"));a=p.parse_args()
 x={"init":{"contract":str(ensure()),"broker_calls":0},"evaluate":{"report":str(evaluate()),"broker_calls":0},"self-test":test()}[a.command];print(json.dumps(x))
if __name__=="__main__":main()
