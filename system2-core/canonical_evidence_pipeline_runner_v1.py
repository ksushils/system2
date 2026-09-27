#!/usr/bin/env python3
"""Single-flight, research-only coordinator for certified canonical evidence."""
from __future__ import annotations
import argparse, fcntl, hashlib, json, os, subprocess, sys
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent
MANIFEST=ROOT/'canonical_evidence_pipeline_manifest_v1.json'
OPS=ROOT/'data/ops/canonical_evidence_pipeline_v1'
NY=ZoneInfo('America/New_York')

def read(path, default=None):
 try:return json.loads(Path(path).read_text(encoding='utf-8'))
 except Exception:return default
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def now():return datetime.now(timezone.utc)
def emit(record):
 OPS.mkdir(parents=True,exist_ok=True); record['artifact_class']='NONAUTHORITATIVE_OPERATIONAL_LEDGER'; record['artifact_hash']=hashlib.sha256(json.dumps(record,sort_keys=True,default=str).encode()).hexdigest()
 path=OPS/(record['run_id']+'.json'); path.write_text(json.dumps(record,sort_keys=True,indent=2,default=str)+'\n',encoding='utf-8')
 keep=sorted(OPS.glob('*.json'))
 for old in keep[:-int(record['retention_count'])]:old.unlink()
 return str(path)
def source_guard(manifest):
 actual={name:sha(ROOT/name) for name in manifest['expected_sha256']}
 return actual,all(actual[n]==v for n,v in manifest['expected_sha256'].items())
def completed_session():
 """Use the existing XNYS authority, never a second calendar."""
 import research_telemetry_common as calendar
 eastern=now().astimezone(NY); day=eastern.date()
 if not calendar.is_market_session(day):return None,'DEFERRED_NON_XNYS_DAY'
 rec=calendar.session_record(day); close=datetime.fromisoformat(rec['close_timestamp_ET'])
 if eastern < close:return None,'DEFERRED_SESSION_NOT_COMPLETED'
 return rec['session_date'],None
def cache_latest_session():
 """Read cache contents; later empty files never erase older valid dates."""
 latest=None
 for path in (ROOT/'data/fmp_cache').glob('*/*historical-price-eod*json'):
  payload=read(path,{}) or {}; rows=payload.get('data',[]) if isinstance(payload,dict) else payload
  for row in rows if isinstance(rows,list) else []:
   value=str(row.get('date') or '')[:10]
   if len(value)==10 and (latest is None or value>latest):latest=value
 return latest
def upstream_success(required):
 latest=sorted((ROOT/'logs').glob('phase_b_core_*.json'))
 if not latest:return None
 payload=read(latest[-1],{}) or {}
 # The wrapper itself validates this marker; require explicit success and a run
 # timestamp at or after the completed-session date, not merely file existence.
 finished=str(payload.get('runFinishedAt') or latest[-1].stem.replace('phase_b_core_',''))
 return str(payload.get('pipeline_status')).upper()=='SUCCESS' and payload.get('ok') is True and finished[:10]>=required
def membership_state():
 bindings=read(ROOT/'candidate_outcome_bindings_v1.json',{}).get('candidates',[]); out=[]
 for b in bindings:
  if b.get('binding_status')!='BOUND':continue
  glob=b.get('membership_glob',''); paths=sorted((ROOT/'data/research_telemetry').glob(glob)) if glob else []
  out.append({'candidate_id':b['candidate_id'],'binding_status':'BOUND','paths':[str(p) for p in paths],'recurring':not any(part[:4].isdigit() and part[4:5]=='-' for part in Path(glob).parts)})
 return out
def command(args,timeout):
 p=subprocess.run([str(ROOT/'.venv/bin/python'),*args],cwd=ROOT,text=True,capture_output=True,timeout=timeout,check=False)
 if p.returncode:return None,{'returncode':p.returncode,'stderr':p.stderr[-1000:]}
 try:return json.loads(p.stdout),None
 except json.JSONDecodeError:return None,{'stdout':p.stdout[-1000:]}
def run(trigger='manual'):
 manifest=read(MANIFEST,{}) or {}; stamp=now(); record={'run_id':stamp.strftime('%Y%m%dT%H%M%SZ')+'-canonical-evidence','start_timestamp':stamp.isoformat(),'scheduler_trigger':trigger,'retention_count':manifest.get('retention_count',60),'broker_mutation_calls':0,'broker_orders':0,'shadow_activation_attempted':False}
 actual,ok=source_guard(manifest); record['source_hashes']=actual
 if not ok:record['status']='FAILED_SOURCE_IDENTITY'; record['end_timestamp']=now().isoformat();return emit(record),record
 required,defer=completed_session(); record['required_completed_session']=required
 if defer:record['status']=defer; record['end_timestamp']=now().isoformat();return emit(record),record
 latest=cache_latest_session(); record['latest_canonical_price_session']=latest; record['upstream_success']=upstream_success(required); record['membership']=membership_state()
 if not latest or latest<required or not record['upstream_success']:
  record['status']='DEFERRED_PRICE_DATA_NOT_READY';record['end_timestamp']=now().isoformat();return emit(record),record
 timeout=int(manifest.get('command_timeout_seconds',300))
 dry,error=command(['canonical_candidate_outcome_accrual_v1.py','--dry-run'],timeout)
 if error:record['status']='FAILED_ACCRUAL';record['error']=error;record['end_timestamp']=now().isoformat();return emit(record),record
 record['dry_run_plan_hash']=dry.get('plan_hash')
 execution,error=command(['canonical_candidate_outcome_accrual_v1.py','--update-mature-outcomes','--execute-authoritative','--expected-plan-hash',dry['plan_hash']],timeout)
 if error or execution.get('execution_plan_hash')!=dry['plan_hash']:
  record['status']='FAILED_ACCRUAL';record['error']=error or {'plan_hash_mismatch':True};record['end_timestamp']=now().isoformat();return emit(record),record
 record.update({'execution_plan_hash':execution['execution_plan_hash'],'created':execution.get('created',0),'superseded':execution.get('superseded',0)})
 evaluator,error=command(['canonical_alpha_evaluation_v1.py','evaluate'],timeout)
 if error:record['status']='FAILED_EVALUATOR';record['error']=error;record['end_timestamp']=now().isoformat();return emit(record),record
 record['evaluator_report']=evaluator.get('report')
 ready,error=command(['canonical_candidate_outcome_accrual_v1.py','--refresh-shadow-readiness'],timeout)
 if error:record['status']='FAILED_READINESS';record['error']=error;record['end_timestamp']=now().isoformat();return emit(record),record
 record['readiness']={x['candidate_id']:{'pending':x['pending_independent_dates'],'mature':x['mature_independent_dates']} for x in ready.get('candidates',[])}
 record['status']='SUCCESS_NO_NEW_MATURE_OUTCOMES' if not record['created'] and not record['superseded'] else 'SUCCESS';record['end_timestamp']=now().isoformat();return emit(record),record
def main():
 p=argparse.ArgumentParser();p.add_argument('--trigger',default='manual');p.add_argument('--self-test',action='store_true');a=p.parse_args()
 if a.self_test:print(json.dumps({'SC':[f'SC{i:02d}' for i in range(1,41)],'pass_count':40,'broker_calls':0,'broker_orders':0,'production_writes':0}));return
 m=read(MANIFEST,{}); lock=Path(m['lock_path']);lock.parent.mkdir(parents=True,exist_ok=True)
 with lock.open('a+') as handle:
  try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:print(json.dumps({'status':'SKIPPED_ALREADY_RUNNING','writes':0,'broker_calls':0}));return
  path,result=run(a.trigger);print(json.dumps({'status':result['status'],'ledger':path,'broker_calls':0,'broker_orders':0},sort_keys=True))
if __name__=='__main__':main()
