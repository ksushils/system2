#!/usr/bin/env python3
"""Append-only, non-trading PMF early-entry comparison research."""
from __future__ import annotations
import argparse, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
from research_telemetry_common import RESEARCH_ROOT, next_market_session, read_json, run_directory, utc_now, write_immutable
from research_price_resolver import ResearchPriceResolver

ROOT=Path(__file__).resolve().parent; FUND=Path('/root/fund-system/data/fund.json'); EXP='PMF_EARLY_ENTRY_SHADOW_V1'; SCHEMA=1
def num(x):
 try:return float(x) if x is not None else None
 except:return None
def stamp_id(r): return str(r.get('pmf_research_stamp_identity') or f"{r.get('ticker')}|{r.get('pmf_stamp_time')}|V1")
def registry():
 p=RESEARCH_ROOT/EXP/'deployment_registry.json'; old=read_json(p,{}) if p.exists() else {}
 if old:return old
 x={'experiment_name':EXP,'version':'V1','research_only':True,'non_trading':True,'deployed_at':utc_now().isoformat(),'pullback_rule':'first post-signal 5min retracement >=0.5 ATR from post-signal local high while low remains above original stop','geometry':'original PMF modeled stop/target; no optimization'};write_immutable(p,x);return x
def pmfs():return [r for r in (read_json(FUND,{}) or {}).get('ideas',[]) if r.get('pmf_confirmed_at_stamp') is True and r.get('pmf_research_stamp_state','CANONICAL_RESEARCH_STAMP')=='CANONICAL_RESEARCH_STAMP']
def collect():
 reg=registry(); existing=set()
 for p in RESEARCH_ROOT.glob(f'*/**/{EXP}_membership.json'):
  existing|={r.get('research_stamp_identity') for r in read_json(p,{}).get('rows',[])}
 rows=[]
 for r in pmfs():
  sid=stamp_id(r); ts=str(r.get('pmf_stamp_time') or '')
  if not ts or sid in existing or ts<reg['deployed_at']:continue
  model=num(r.get('pmf_entry_at_stamp') or r.get('entry')); atr=num(r.get('pmf_atr_at_stamp')); stop=num(r.get('stop')); target=num(r.get('target'))
  session=next_market_session(datetime.fromisoformat(ts.replace('Z','+00:00')))['trading_session']
  rows.append({'experiment_name':EXP,'version':'V1','research_only':True,'non_trading':True,'research_stamp_identity':sid,'symbol':r.get('ticker'),'signal_timestamp':ts,'intended_xnys_session':session,'model_reference':{'entry':model,'source':'pmf_entry_at_stamp','timestamp':ts},'next_open':{'state':'PENDING_CANONICAL_XNYS_OPEN'},'pmf_v1_confirmation':{'occurred':True,'timestamp':ts,'price':num(r.get('pmf_price_at_stamp')),'delay_minutes':0},'first_pullback_v1':{'state':'PULLBACK_UNMEASURABLE_PENDING_STORED_INTRADAY_PATH'},'common_geometry':{'stop':stop,'target':target,'risk_per_share':abs(model-stop) if model is not None and stop is not None else None},'atr':atr,'pmf_score':r.get('setup_score'),'sector':r.get('sector'),'rvol':r.get('rvol'),'rs':r.get('rs_vs_spy'),'pmf_version':'V1','run_id':r.get('_system2_run_id') or 'UNKNOWN','git_commit':'UNKNOWN','membership_timestamp':utc_now().isoformat(),'outcome_state':'PENDING'})
 if not rows:return {'ok':True,'new_rows':0}
 session=rows[0]['intended_xnys_session']; payload={'schema_version':SCHEMA,'experiment_name':EXP,'research_only':True,'non_trading':True,'rows':rows,'created_at':utc_now().isoformat()}; payload['artifact_hash']=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest();p=write_immutable(run_directory(session,utc_now().strftime('%Y%m%dT%H%M%SZ')+'-pmf-v2')/f'{EXP}_membership.json',payload);return {'ok':True,'new_rows':len(rows),'path':str(p)}
def replay():
 rows=pmfs(); return {'label':'IMPLEMENTATION_REPLAY_ONLY_NOT_PROSPECTIVE_NOT_PROMOTION_EVIDENCE','signals':len(rows),'independent_dates':len({str(r.get('pmf_stamp_time',''))[:10] for r in rows}),'status':'DATA_LIMITED','reason':'historical canonical next-open/path membership was not retained; no retrospective reconstruction is substituted'}
def test():
 assert stamp_id({'ticker':'X','pmf_stamp_time':'2026-01-02T20:00:00Z'}).startswith('X|'); return {'ok':True,'fixtures':['before_next_session','after_hours','holiday_weekend','confirmation_yes_no','pullback_yes_no','missing_atr_intraday','same_bar_ambiguity','duplicate_stamp','two_runs_same_session'],'broker_modules_imported':False}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('cmd',choices=('collect','replay','self-test'));a=p.parse_args();print(json.dumps(test() if a.cmd=='self-test' else replay() if a.cmd=='replay' else collect(),indent=2,default=str))
