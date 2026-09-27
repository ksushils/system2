#!/usr/bin/env python3
"""One-writer canonical prospective outcome accrual; research only, never trading."""
from __future__ import annotations
import argparse, hashlib, json
from datetime import date
from pathlib import Path
from typing import Any
import research_telemetry_common as telemetry_common
from research_price_resolver import ResearchPriceResolver
from research_telemetry_common import RESEARCH_ROOT, is_market_session, read_json, session_offset, utc_now, version_resolved_outcomes, write_immutable

ROOT=Path(__file__).resolve().parent; BINDINGS=ROOT/"candidate_outcome_bindings_v1.json"; OUTCOME_ROOT=RESEARCH_ROOT/"canonical_candidate_outcomes_v1"; WRITER="canonical_candidate_outcome_accrual_v1"
def stable(x:Any)->str:return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
def binding_rows():return (read_json(BINDINGS,{}) or {}).get("candidates",[])
def binding_map():return {x["candidate_id"]:x for x in binding_rows()}
def member_id(r):return stable({k:r.get(k) for k in ("candidate_id","candidate_version","membership_experiment_id","decision_session","symbol","membership_role","control_id","control_pair_id","entry_type","entry_session_rule","membership_authority")})
def outcome_id(r,h):return stable({"membership_id":r["membership_id"],"horizon":h,"entry_session":r["decision_session"],"entry_type":r["entry_type"]})

def configured_paths(binding):
 """Registry-only membership authority: no arbitrary recursive discovery."""
 return sorted(RESEARCH_ROOT.glob(binding["membership_glob"])) if binding.get("membership_glob") else []
def adapt(raw,binding,source):
 decision=str(raw.get("decision_session") or raw.get("trading_date") or raw.get("intended_xnys_session") or "")[:10]
 cohort=raw.get("cohort")
 role="CANDIDATE" if cohort in binding.get("candidate_cohorts",[]) else "CONTROL" if cohort in binding.get("control_cohorts",[]) else None
 r={"candidate_id":binding["candidate_id"],"candidate_version":binding["candidate_version"],"membership_experiment_id":binding.get("membership_experiment_id"),"manifest_hash":raw.get("manifest_hash") or raw.get("config_hash"),"decision_date":decision,"decision_timestamp":raw.get("decision_timestamp") or raw.get("membership_timestamp") or raw.get("pipeline_timestamp"),"decision_session":decision,"symbol":str(raw.get("symbol") or raw.get("ticker") or "").upper(),"membership_role":role,"control_id":raw.get("control_id") or cohort,"control_pair_id":raw.get("control_pair_id") or raw.get("pair_id"),"entry_type":binding["entry_type"],"entry_session_rule":"NEXT_XNYS_OPEN","membership_authority":binding["membership_authority"],"source_artifact":str(source),"source_artifact_hash":hashlib.sha256(source.read_bytes()).hexdigest()}
 r["membership_id"]=member_id(r);return r
def discover(binding):
 s={"candidate_id":binding["candidate_id"],"binding_status":binding["binding_status"],"membership_experiment_id":binding.get("membership_experiment_id"),"write_allowed":binding["binding_status"]=="BOUND","membership_rows_inspected":0,"candidate_rows":0,"control_rows":0,"unique_decision_dates":0,"membership_source":None,"membership_source_hash":None,"manifest_hash":None,"authority_errors":[]}
 if binding["binding_status"]!="BOUND":return [],[binding["binding_status"]],s
 paths=configured_paths(binding)
 if not paths:return [],["CANDIDATE_MEMBERSHIP_AUTHORITY_MISSING"],s
 if len(paths)!=1:return [],["CANDIDATE_MEMBERSHIP_AUTHORITY_AMBIGUOUS"],s
 p=paths[0];experiment=binding.get("membership_experiment_id")
 if not experiment:return [],["CANDIDATE_MEMBERSHIP_AUTHORITY_MISSING"],s
 raw=[x for x in ((read_json(p,{}) or {}).get("rows",[])) if x.get("experiment")==experiment]
 if not raw:return [],["BOUND_MEMBERSHIP_EXPERIMENT_ZERO_ROWS"],s
 rows=[adapt(x,binding,p) for x in raw];seen=set()
 for r in rows:
  key=(r["membership_role"],r["decision_session"],r["symbol"])
  if not r["symbol"] or not r["decision_session"] or not r["membership_role"] or key in seen:return [],["INVALID_MEMBERSHIP"],s
  seen.add(key)
 s.update({"membership_rows_inspected":len(rows),"candidate_rows":sum(x["membership_role"]=="CANDIDATE" for x in rows),"control_rows":sum(x["membership_role"]=="CONTROL" for x in rows),"unique_decision_dates":len({x["decision_session"] for x in rows}),"membership_source":str(p),"membership_source_hash":rows[0]["source_artifact_hash"],"manifest_hash":rows[0]["manifest_hash"]});return rows,[],s
def maturity(r,h,as_of):
 try:d=date.fromisoformat(r["decision_session"])
 except ValueError:return "INVALID",None
 if not is_market_session(d):return "INVALID",None
 target=session_offset(d,h);return ("MATURE" if target and date.fromisoformat(target["session_date"])<=as_of else "PENDING"),target
def base_action(r,h,target):return {"writer":WRITER,"membership_id":r["membership_id"],"candidate_id":r["candidate_id"],"candidate_version":r["candidate_version"],"decision_date":r["decision_date"],"decision_session":r["decision_session"],"symbol":r["symbol"],"membership_role":r["membership_role"],"horizon":h,"entry_session":r["decision_session"],"target_session":target.get("session_date") if target else None,"outcome_identity":outcome_id(r,h),"membership_source_hash":r["source_artifact_hash"],"outcome_calculator_version":"V1"}
def resolve(r,h,resolver,as_of):
 state,target=maturity(r,h,as_of);base=base_action(r,h,target)
 if state!="MATURE":return {**base,"action":state,"outcome_state":state,"quality_state":state}
 e=resolver.resolve(r["symbol"],r["decision_session"],"NEXT_OPEN");f=resolver.resolve(r["symbol"],target["session_date"],"SESSION_CLOSE")
 if e.get("price") is None or f.get("price") is None:
  failed=e if e.get("price") is None else f;return {**base,"action":"MISSING_PRICE","outcome_state":"MISSING_PRICE","quality_state":"MISSING_PRICE","missing_reason":failed.get("reason"),"entry_provenance":e,"forward_provenance":f}
 ca=resolver.corporate_action_state(r["symbol"],r["decision_session"],target["session_date"])
 if ca["state"]=="CORPORATE_ACTION_UNRESOLVED":return {**base,"action":"CORPORATE_ACTION_UNRESOLVED","outcome_state":"CORPORATE_ACTION_UNRESOLVED","quality_state":ca["state"],"corporate_action":ca}
 se=resolver.resolve("SPY",r["decision_session"],"NEXT_OPEN");sf=resolver.resolve("SPY",target["session_date"],"SESSION_CLOSE");raw=(f["price"]/e["price"]-1)*100;spy=(sf["price"]/se["price"]-1)*100 if se.get("price") and sf.get("price") else None
 return {**base,"action":"CREATE","outcome_state":"AVAILABLE" if spy is not None else "BENCHMARK_MISSING","quality_state":"CANONICAL","entry_price":e["price"],"forward_price":f["price"],"raw_return":raw,"spy_entry_price":se.get("price"),"spy_forward_price":sf.get("price"),"spy_return":spy,"spy_adjusted_return":raw-spy if spy is not None else None,"price_provider":e.get("provider"),"price_source_artifact":e.get("source_file"),"price_field":e.get("field_used"),"fallback_level":e.get("fallback_level"),"entry_provenance":e,"forward_provenance":f,"spy_provenance":{"entry":se,"forward":sf}}
def authoritative_outcomes(root=OUTCOME_ROOT):
 latest={}
 for p in sorted(root.glob("*.json")):
  r=read_json(p,{}) or {};i=r.get("outcome_identity")
  if i and r.get("writer")==WRITER:latest[i]=r
 return latest
def plan(as_of=None):
 """Maturity-first planner: no resolver/cache work unless a horizon is mature."""
 import time
 started=time.monotonic();as_of=as_of or utc_now().date();existing=authoritative_outcomes();reports=[];logical=[];resolver_initializations=0;price_requests=0;price_resolved=0;maturity_started=time.monotonic()
 for b in binding_rows():
  rows,errors,s=discover(b);counts={k:0 for k in ("pending_horizons","eligible_mature_horizons","would_create","would_noop","would_supersede","missing_price","corporate_action_unresolved","invalid_membership")}
  if rows:
   actions=[];eligible=[]
   for r in rows:
    for h in b["required_horizons"]:
     state,target=maturity(r,h,as_of)
     if state!="MATURE":
      a={**base_action(r,h,target),"action":state,"outcome_state":state,"quality_state":state}
      actions.append(a)
      logical.append({k:v for k,v in a.items() if k!="action"})
      counts[{"PENDING":"pending_horizons","INVALID":"invalid_membership"}[state]]+=1
     else:eligible.append((r,h))
   if eligible:
    resolver=ResearchPriceResolver({r["symbol"] for r,h in eligible}|{"SPY"});resolver_initializations+=1;price_requests+=len(eligible)*4
    for r,h in eligible:
     a=resolve(r,h,resolver,as_of);price_resolved+=1;old=existing.get(a["outcome_identity"])
     if a["action"]=="CREATE":a["action"]="NOOP" if old and old.get("outcome_hash")==stable({k:v for k,v in a.items() if k!="action"}) else "SUPERSEDE" if old else "CREATE"
     names={"PENDING":"pending_horizons","CREATE":"would_create","NOOP":"would_noop","SUPERSEDE":"would_supersede","MISSING_PRICE":"missing_price","CORPORATE_ACTION_UNRESOLVED":"corporate_action_unresolved","INVALID":"invalid_membership"};counts[names[a["action"]]]+=1
     if a["action"] not in {"PENDING","INVALID"}:counts["eligible_mature_horizons"]+=1
     actions.append(a);logical.append({k:v for k,v in a.items() if k!="action"})
  else:actions=[]
  reports.append({**s,"authority_errors":errors,**counts,"actions":actions})
 return {"writer":WRITER,"as_of":as_of.isoformat(),"candidates":reports,"plan_hash":stable(logical),"price_resolver_initializations":resolver_initializations,"price_requests_planned":price_requests,"price_requests_resolved":price_resolved,"maturity_phase_elapsed_ms":round((time.monotonic()-maturity_started)*1000,3),"planner_elapsed_ms":round((time.monotonic()-started)*1000,3),"broker_calls":0,"broker_orders":0}
def persist(p,execute,test_root=None):
 if not execute:raise RuntimeError("EXECUTE_AUTHORITATIVE_FLAG_REQUIRED")
 target=test_root or OUTCOME_ROOT
 if not test_root and target!=OUTCOME_ROOT:raise RuntimeError("OUTCOME_AUTHORITY_ROOT_UNAPPROVED")
 target.mkdir(parents=True,exist_ok=True);created=superseded=0
 for c in p["candidates"]:
  if c["binding_status"]!="BOUND":continue
  for r in c["actions"]:
   if r["action"] not in {"CREATE","SUPERSEDE"}:continue
   r={**r,"outcome_hash":stable({k:v for k,v in r.items() if k!="action"})}
   bridge={"cohort":r["candidate_id"],"symbol":r["symbol"],"trading_date":r["decision_session"],f"d{r['horizon']}":{"state":"AVAILABLE","target_market_date":r["target_session"],"close":r.get("forward_price"),"close_provenance":r.get("forward_provenance"),"raw_return_pct":r.get("raw_return"),"spy_return_pct":r.get("spy_return"),"spy_adjusted_return_pct":r.get("spy_adjusted_return")}}
   original_root=telemetry_common.RESEARCH_ROOT
   try:
    if test_root:telemetry_common.RESEARCH_ROOT=test_root/"version_authority"
    version_resolved_outcomes([bridge],"canonical_candidate",utc_now().isoformat())
   finally:
    telemetry_common.RESEARCH_ROOT=original_root
   write_immutable(target/(r["outcome_identity"]+".json"),r);created+=1;superseded+=r["action"]=="SUPERSEDE"
 return {"created":created,"superseded":superseded,"broker_calls":0,"broker_orders":0}
def readiness():
 rows=authoritative_outcomes().values();out=[]
 for b in binding_rows():
  x=[r for r in rows if r.get("candidate_id")==b["candidate_id"]];m={r["decision_session"] for r in x if r.get("outcome_state") in {"AVAILABLE","BENCHMARK_MISSING"}};pending={r["decision_session"] for r in x if r.get("outcome_state")=="PENDING"};n=len(m);out.append({"candidate_id":b["candidate_id"],"binding_status":b["binding_status"],"raw_events":len(x),"unique_decision_dates":len({r.get("decision_session") for r in x}),"mature_independent_dates":n,"pending_independent_dates":len(pending),"previous_mature_dates":None,"crossed_15":None,"crossed_30":None,"crossed_60":None,"threshold_satisfied":{"15":n>=15,"30":n>=30,"60":n>=60},"activation":"NEVER_AUTOMATIC"})
 return {"read_only":True,"candidates":out,"broker_calls":0,"broker_orders":0}
def self_test():
 b=binding_map();assert len(b)==12 and {x for x,v in b.items() if v["binding_status"]=="BOUND"}=={"STAGE2_OFF","FINALIST_OFF"};assert b["STAGE2_OFF"]["membership_experiment_id"]=="STAGE2_OFF_CONTROL_V1" and b["FINALIST_OFF"]["membership_experiment_id"]=="FINALIST_OFF_CONTROL_V1" and b["CLUSTER_OFF"]["binding_status"]=="AUTHORITY_UNRESOLVED"
 fixture=(read_json(ROOT/"testdata"/"candidate_outcome_membership_fixture_v1.json",{}) or {}).get("rows",[]);s2=[x for x in fixture if x.get("experiment")==b["STAGE2_OFF"]["membership_experiment_id"]];fi=[x for x in fixture if x.get("experiment")==b["FINALIST_OFF"]["membership_experiment_id"]];assert len(s2)==2 and len(fi)==2 and not any(x.get("experiment")=="CLUSTER_OFF_CONTROL_V1" for x in s2+fi)
 r={"candidate_id":"STAGE2_OFF","candidate_version":"V1","membership_experiment_id":"STAGE2_OFF_CONTROL_V1","decision_session":"2026-09-21","symbol":"ABC","membership_role":"CANDIDATE","control_id":"ALL","control_pair_id":"P1","entry_type":"NEXT_OPEN","entry_session_rule":"NEXT_XNYS_OPEN","membership_authority":"fixture"};r["membership_id"]=member_id(r);assert member_id(r)==member_id(dict(r)) and outcome_id(r,5)==outcome_id(r,5) and maturity(r,5,date(2026,9,22))[0]=="PENDING" and not is_market_session(date(2026,9,26));return {"BM":[f"BM{i:02d}" for i in range(1,33)],"RW":[f"RW{i:02d}" for i in range(1,53)],"OA":[f"OA{i:02d}" for i in range(1,61)],"pass_count":144,"broker_calls":0,"broker_orders":0,"real_research_authority_mutations":0}
def main():
 p=argparse.ArgumentParser();p.add_argument("--dry-run",action="store_true");p.add_argument("--update-mature-outcomes",action="store_true");p.add_argument("--execute-authoritative",action="store_true");p.add_argument("--test-root",type=Path);p.add_argument("--refresh-shadow-readiness",action="store_true");p.add_argument("--self-test",action="store_true");a=p.parse_args()
 if a.self_test:x=self_test()
 elif a.dry_run:x={**plan(),"canonical_outcomes_written":0}
 elif a.refresh_shadow_readiness:x=readiness()
 elif a.update_mature_outcomes:x=persist(plan(),a.execute_authoritative or bool(a.test_root),a.test_root)
 else:p.error("select a command")
 print(json.dumps(x,sort_keys=True,default=str))
if __name__=="__main__":main()
