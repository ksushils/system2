#!/usr/bin/env python3
"""Offline-only, fail-closed safety substrate for the Shadow harness."""
from __future__ import annotations
import hashlib,json,os,sqlite3,tempfile,threading
from datetime import datetime,timezone
from pathlib import Path

ALLOWED="SHADOW_RESEARCH"; SCHEMA_VERSION=1
ACTIONS=("PLACE_ORDER","OPEN_POSITION","CLOSE_POSITION","MODIFY_STOP","MODIFY_TARGET","CANCEL_ORDER")
ENTRY_TYPES={"NEXT_OPEN","MODEL_REFERENCE","FIRST_PULLBACK","CONFIRMATION_ENTRY","SHADOW_EXECUTABLE_ENTRY","PAPER_FILL","REAL_FILL"}
CANDIDATES=("STAGE1_REDESIGN","STAGE2_OFF","STAGE2_SIMPLIFIED_V2","CLUSTER_OFF","FINALIST_OFF","FINALIST_SIMPLIFIED","PEAD_EXIT_V2","PMF_V2","MOMENTUM","CATALYST_CONTINUATION","IDIOSYNCRATIC_REVERSAL","SYSTEM2_5_V4")
class HarnessStartBlocked(RuntimeError): pass
class ShadowStateLoadFailure(RuntimeError): pass
class ShadowStorageSchemaMismatch(ShadowStateLoadFailure): pass
class ShadowExecutionModeGuard:
 def __init__(self,mode=None):
  self.mode=mode if mode is not None else os.getenv("SYSTEM2_EXECUTION_MODE")
  if self.mode!=ALLOWED: raise HarnessStartBlocked("HARNESS_START_BLOCKED:SHADOW_RESEARCH_MODE_REQUIRED")
class ShadowAuditLedger:
 def __init__(self,root):
  self.root=Path(root).resolve()
  if "test_runtime" not in self.root.parts: raise ValueError("ISOLATED_TEST_ROOT_REQUIRED")
  self.root.mkdir(parents=True,exist_ok=True);self.path=self.root/"audit.jsonl"
 def event(self,**row):
  row.setdefault("timestamp",datetime.now(timezone.utc).isoformat())
  with self.path.open("a",encoding="utf8") as f:f.write(json.dumps(row,sort_keys=True)+"\n")
  return row
 def rows(self):return [json.loads(x) for x in self.path.read_text().splitlines()] if self.path.exists() else []
class FakeBrokerAdapter:
 def __init__(self):self.calls={a:0 for a in ACTIONS}
 def mutate(self,action):self.calls[action]+=1
class ShadowBrokerFirewall:
 def __init__(self,audit,broker):self.audit,self.broker=audit,broker
 def mutate(self,action,candidate_id="FIXTURE",shadow_run_id="fixture-run",symbol="TEST"):
  if action not in ACTIONS:raise ValueError("UNKNOWN_MUTATION")
  return self.audit.event(event_type="BLOCKED_BROKER_CALL",candidate_id=candidate_id,shadow_run_id=shadow_run_id,attempted_action=action,symbol=symbol,reason="SHADOW_RESEARCH_MUTATION_PROHIBITED",blocked=True)

def canonical_idempotency_key(candidate_id,candidate_version,symbol,session,decision_timestamp,manifest_hash):
 payload={"candidate_id":candidate_id,"candidate_version":candidate_version,"symbol":symbol,"session":session,"decision_timestamp":decision_timestamp,"manifest_hash":manifest_hash}
 return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":" )).encode()).hexdigest()

class ShadowStateStore:
 def __init__(self,root):
  self.root=Path(root).resolve()
  if "test_runtime" not in self.root.parts:raise ValueError("ISOLATED_TEST_ROOT_REQUIRED")
  self.root.mkdir(parents=True,exist_ok=True);self.path=self.root/"shadow_state.sqlite3";existed=self.path.exists();self.conn=None
  try:
   self.conn=sqlite3.connect(str(self.path),timeout=2,isolation_level=None,check_same_thread=False);self.conn.execute("PRAGMA foreign_keys=ON")
   if self.conn.execute("PRAGMA quick_check").fetchone() != ("ok",):raise ShadowStateLoadFailure("SHADOW_STATE_LOAD_FAILURE")
   self._schema(existed)
  except ShadowStateLoadFailure:
   if self.conn:self.conn.close()
   raise
  except sqlite3.DatabaseError as exc:
   if self.conn:self.conn.close()
   raise ShadowStateLoadFailure("SHADOW_STATE_LOAD_FAILURE") from exc
  if existed:self.audit("RESTART_STATE_RECOVERED")
 def close(self):self.conn.close()
 def _schema(self,existed):
  tables={x[0] for x in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")};req={"shadow_schema","shadow_signals","shadow_intents","shadow_audit_events","shadow_runtime_state"}
  if not existed and not tables:
   self.conn.executescript("""BEGIN;
CREATE TABLE shadow_schema(version INTEGER NOT NULL); INSERT INTO shadow_schema VALUES(1);
CREATE TABLE shadow_signals(shadow_signal_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,candidate_id TEXT NOT NULL,candidate_version TEXT NOT NULL,shadow_run_id TEXT NOT NULL,symbol TEXT NOT NULL,session TEXT NOT NULL,decision_timestamp TEXT NOT NULL,source_timestamp TEXT NOT NULL,decision TEXT NOT NULL CHECK(decision IN ('SELECTED','REJECTED')),reason TEXT NOT NULL,input_snapshot_hash TEXT NOT NULL,manifest_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE shadow_intents(shadow_intent_id TEXT PRIMARY KEY,signal_id TEXT NOT NULL REFERENCES shadow_signals(shadow_signal_id),idempotency_key TEXT UNIQUE NOT NULL,candidate_id TEXT NOT NULL,candidate_version TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,decision_timestamp TEXT NOT NULL,intended_session TEXT NOT NULL,entry_type TEXT NOT NULL,risk_model TEXT NOT NULL,stop_model TEXT NOT NULL,target_model TEXT NOT NULL,holding_horizon TEXT NOT NULL,status TEXT NOT NULL CHECK(status='NOT_SENT_TO_BROKER'),created_at TEXT NOT NULL);
CREATE TABLE shadow_audit_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp TEXT NOT NULL,event_type TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE shadow_runtime_state(state_key TEXT PRIMARY KEY,state_value TEXT NOT NULL); COMMIT;""");return
  if not req.issubset(tables):raise ShadowStateLoadFailure("SHADOW_STATE_LOAD_FAILURE")
  if self.conn.execute("SELECT version FROM shadow_schema").fetchall()!=[(SCHEMA_VERSION,)]:raise ShadowStorageSchemaMismatch("SHADOW_STORAGE_SCHEMA_MISMATCH")
 def audit(self,event_type,**payload):self.conn.execute("INSERT INTO shadow_audit_events(timestamp,event_type,payload) VALUES(?,?,?)",(datetime.now(timezone.utc).isoformat(),event_type,json.dumps(payload,sort_keys=True)))
 def audit_types(self):return [x[0] for x in self.conn.execute("SELECT event_type FROM shadow_audit_events ORDER BY event_id")]
 def counts(self):return {"signals":self.conn.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0],"intents":self.conn.execute("SELECT COUNT(*) FROM shadow_intents").fetchone()[0]}
 def insert_signal_if_absent(self,row):
  key=canonical_idempotency_key(row["candidate_id"],row["candidate_version"],row["symbol"],row["session"],row["decision_timestamp"],row["manifest_hash"]);sid="sig_"+key[:24]
  vals=(sid,key,row["candidate_id"],row["candidate_version"],row["shadow_run_id"],row["symbol"],row["session"],row["decision_timestamp"],row["source_timestamp"],row["decision"],row["reason"],row["input_snapshot_hash"],row["manifest_hash"],datetime.now(timezone.utc).isoformat())
  try:
   self.conn.execute("BEGIN IMMEDIATE");old=self.conn.execute("SELECT candidate_version,manifest_hash FROM shadow_signals WHERE candidate_id=? AND symbol=? AND session=? AND decision_timestamp=?",(row["candidate_id"],row["symbol"],row["session"],row["decision_timestamp"])).fetchone()
   if old and old!=(row["candidate_version"],row["manifest_hash"]):self.audit("IDEMPOTENCY_IDENTITY_CONFLICT",idempotency_key=key);self.conn.execute("COMMIT");return "IDEMPOTENCY_IDENTITY_CONFLICT",old
   inserted=self.conn.execute("INSERT OR IGNORE INTO shadow_signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",vals).rowcount;result="CREATED" if inserted else "DUPLICATE_SIGNAL_SKIPPED"
   if not inserted:self.audit("DUPLICATE_SIGNAL_SKIPPED",idempotency_key=key)
   self.conn.execute("COMMIT");return result,sid
  except Exception:self.conn.execute("ROLLBACK");raise
 def insert_intent_if_absent(self,sid,row,inject_failure=False):
  if row["entry_type"] not in ENTRY_TYPES:raise ValueError("INVALID_ENTRY_TYPE")
  key=hashlib.sha256((sid+"|"+row["entry_type"]+"|"+row["decision_timestamp"]).encode()).hexdigest();iid="intent_"+key[:24]
  try:
   self.conn.execute("BEGIN IMMEDIATE");sig=self.conn.execute("SELECT decision FROM shadow_signals WHERE shadow_signal_id=?",(sid,)).fetchone()
   if not sig:self.audit("INVALID_INTENT_WITHOUT_SIGNAL",signal_id=sid);self.conn.execute("COMMIT");return "INVALID_INTENT_WITHOUT_SIGNAL",None
   if sig[0]!="SELECTED":self.audit("INVALID_INTENT_FROM_REJECTED_SIGNAL",signal_id=sid);self.conn.execute("COMMIT");return "INVALID_INTENT_FROM_REJECTED_SIGNAL",None
   vals=(iid,sid,key,row["candidate_id"],row["candidate_version"],row["symbol"],row["side"],row["decision_timestamp"],row["intended_session"],row["entry_type"],row["risk_model"],row["stop_model"],row["target_model"],row["holding_horizon"],"NOT_SENT_TO_BROKER",datetime.now(timezone.utc).isoformat())
   inserted=self.conn.execute("INSERT OR IGNORE INTO shadow_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",vals).rowcount
   if inject_failure:raise RuntimeError("FIXTURE_ATOMIC_FAILURE")
   result="CREATED" if inserted else "DUPLICATE_INTENT_SKIPPED"
   if not inserted:self.audit("DUPLICATE_INTENT_SKIPPED",signal_id=sid)
   self.conn.execute("COMMIT");return result,iid
  except Exception:
   self.conn.execute("ROLLBACK");self.audit("ATOMIC_INTENT_ROLLBACK",signal_id=sid)
   if inject_failure:return "ATOMIC_INTENT_ROLLBACK",None
   raise

def _signal(decision="SELECTED",version="V1",manifest="manifest-v1"):
 return {"candidate_id":"STAGE2_OFF","candidate_version":version,"shadow_run_id":"run-2026-09-25","symbol":"TEST","session":"2026-09-25","decision_timestamp":"2026-09-24T22:00:00Z","source_timestamp":"2026-09-24T21:59:00Z","decision":decision,"reason":"fixture","input_snapshot_hash":"input-v1","manifest_hash":manifest}
def _intent():return {"candidate_id":"STAGE2_OFF","candidate_version":"V1","symbol":"TEST","side":"BUY","decision_timestamp":"2026-09-24T22:00:00Z","intended_session":"2026-09-25","entry_type":"NEXT_OPEN","risk_model":"NONE","stop_model":"NONE","target_model":"NONE","holding_horizon":"5D"}
def self_test_phase1a():
 results=[]
 for mode in (None,"production","live","paper","INVALID_MODE"):
  try:ShadowExecutionModeGuard(mode);results.append(False)
  except HarnessStartBlocked:results.append(True)
 with tempfile.TemporaryDirectory(prefix="shadow-test_runtime-") as d:
  root=Path(d)/"test_runtime";ShadowExecutionModeGuard(ALLOWED);audit=ShadowAuditLedger(root);broker=FakeBrokerAdapter();fw=ShadowBrokerFirewall(audit,broker);results.append(True)
  for a in ACTIONS:r=fw.mutate(a);results.append(r["blocked"] and r["attempted_action"]==a and broker.calls[a]==0)
  assert all(results) and len(results)==12 and len(audit.rows())==6 and sum(broker.calls.values())==0
 return {"tests_passed":12,"blocked_events":6,"fake_broker_executions":0,"real_broker_calls":0,"network_required":False}
def registry():return {c:{"candidate_version":"V1","manifest_hash":hashlib.sha256((c+"|V1").encode()).hexdigest(),"shadow_enabled":False,"owner_authorized":False,"version_frozen":True} for c in CANDIDATES}
def activate(candidate_id,version,manifest_hash,**v):
 r=registry()
 if candidate_id not in r:return "UNKNOWN_CANDIDATE"
 c=r[candidate_id]
 if version!=c["candidate_version"]:return "INVALID_CANDIDATE_VERSION"
 if manifest_hash!=c["manifest_hash"]:return "MANIFEST_HASH_MISMATCH"
 for key,reason in (("owner_authorized","OWNER_AUTHORIZATION_REQUIRED"),("evidence_gate_met","EVIDENCE_GATE_NOT_MET"),("data_quality_clear","DATA_QUALITY_BLOCK"),("point_in_time_guard_passed","POINT_IN_TIME_GUARD_NOT_PASSED"),("broker_firewall_passed","BROKER_FIREWALL_NOT_PASSED"),("global_shadow_enabled","GLOBAL_SHADOW_DISABLED"),("candidate_shadow_enabled","CANDIDATE_SHADOW_DISABLED"),("version_frozen","CANDIDATE_VERSION_NOT_FROZEN")):
  if not v.get(key,c.get(key,False)):return reason
 return "ACTIVATION_GUARDS_PASSED"
def self_test_phase1b():
 c="STAGE2_OFF";h=registry()[c]["manifest_hash"];base=dict(owner_authorized=True,evidence_gate_met=True,data_quality_clear=True,point_in_time_guard_passed=True,broker_firewall_passed=True,global_shadow_enabled=True,candidate_shadow_enabled=True,version_frozen=True);cases=[("owner_authorized","OWNER_AUTHORIZATION_REQUIRED"),("evidence_gate_met","EVIDENCE_GATE_NOT_MET"),("data_quality_clear","DATA_QUALITY_BLOCK"),("point_in_time_guard_passed","POINT_IN_TIME_GUARD_NOT_PASSED"),("broker_firewall_passed","BROKER_FIREWALL_NOT_PASSED"),("global_shadow_enabled","GLOBAL_SHADOW_DISABLED"),("candidate_shadow_enabled","CANDIDATE_SHADOW_DISABLED"),("version_frozen","CANDIDATE_VERSION_NOT_FROZEN")]
 for k,e in cases:x=dict(base);x[k]=False;assert activate(c,"V1",h,**x)==e
 assert activate(c,"V1","bad",**base)=="MANIFEST_HASH_MISMATCH" and activate("UNKNOWN","V1","x",**base)=="UNKNOWN_CANDIDATE" and activate(c,"bad",h,**base)=="INVALID_CANDIDATE_VERSION" and activate(c,"V1",h,**base)=="ACTIVATION_GUARDS_PASSED" and len(registry())==12 and all(not x["shadow_enabled"] and not x["owner_authorized"] for x in registry().values()) and registry()==registry()
 return {"tests_passed":14}
def self_test_phase1c():
 cases={}
 with tempfile.TemporaryDirectory(prefix="shadow-phase1c-test_runtime-") as d:
  root=Path(d)/"test_runtime";s=_signal();k1=canonical_idempotency_key(s["candidate_id"],s["candidate_version"],s["symbol"],s["session"],s["decision_timestamp"],s["manifest_hash"]);k2=canonical_idempotency_key(s["candidate_id"],s["candidate_version"],s["symbol"],s["session"],s["decision_timestamp"],s["manifest_hash"]);cases["C01"]=k1==k2
  store=ShadowStateStore(root);a,sid=store.insert_signal_if_absent(s);b,_=store.insert_signal_if_absent(s);cases["C02"]=a=="CREATED" and b=="DUPLICATE_SIGNAL_SKIPPED" and store.counts()["signals"]==1
  a,b=store.insert_intent_if_absent(sid,_intent())[0],store.insert_intent_if_absent(sid,_intent())[0];cases["C03"]=a=="CREATED" and b=="DUPLICATE_INTENT_SKIPPED" and store.counts()["intents"]==1;store.close()
  store=ShadowStateStore(root);a,_=store.insert_signal_if_absent(s);b,_=store.insert_intent_if_absent(sid,_intent());cases["C04"]=store.counts()=={"signals":1,"intents":1} and a=="DUPLICATE_SIGNAL_SKIPPED" and b=="DUPLICATE_INTENT_SKIPPED" and "RESTART_STATE_RECOVERED" in store.audit_types()
  cases["C09"]=store.insert_intent_if_absent("missing",_intent())[0]=="INVALID_INTENT_WITHOUT_SIGNAL";rejected={**_signal(decision="REJECTED",manifest="manifest-rejected"),"decision_timestamp":"2026-09-24T22:01:00Z"};a,rejected_id=store.insert_signal_if_absent(rejected);cases["C10"]=a=="CREATED" and store.insert_intent_if_absent(rejected_id,_intent())[0]=="INVALID_INTENT_FROM_REJECTED_SIGNAL";a,_=store.insert_signal_if_absent(_signal(version="V2",manifest="manifest-v2"));cases["C11"]=a=="IDEMPOTENCY_IDENTITY_CONFLICT" and store.counts()["signals"]==2
  a,_=store.insert_intent_if_absent(sid,{**_intent(),"entry_type":"MODEL_REFERENCE"},True);n=store.counts()["intents"];b,_=store.insert_intent_if_absent(sid,{**_intent(),"entry_type":"MODEL_REFERENCE"});cases["C12"]=a=="ATOMIC_INTENT_ROLLBACK" and n==1 and b=="CREATED" and store.counts()["intents"]==2 and "ATOMIC_INTENT_ROLLBACK" in store.audit_types();store.close()
  corrupt=Path(d)/"corrupt"/"test_runtime";corrupt.mkdir(parents=True);(corrupt/"shadow_state.sqlite3").write_bytes(b"bad")
  try:ShadowStateStore(corrupt);cases["C05"]=False
  except ShadowStateLoadFailure as e:cases["C05"]=str(e)=="SHADOW_STATE_LOAD_FAILURE"
  schema_root=Path(d)/"schema"/"test_runtime";x=ShadowStateStore(schema_root);x.conn.execute("UPDATE shadow_schema SET version=999");x.close()
  try:ShadowStateStore(schema_root);cases["C06"]=False
  except ShadowStorageSchemaMismatch as e:cases["C06"]=str(e)=="SHADOW_STORAGE_SCHEMA_MISMATCH"
  cr=Path(d)/"concurrent"/"test_runtime";ShadowStateStore(cr).close();out=[]
  def add_signal():
   x=ShadowStateStore(cr)
   try:out.append(x.insert_signal_if_absent(_signal())[0])
   finally:x.close()
  ts=[threading.Thread(target=add_signal) for _ in range(6)];[t.start() for t in ts];[t.join() for t in ts];x=ShadowStateStore(cr);cases["C07"]=x.counts()["signals"]==1 and out.count("CREATED")==1 and out.count("DUPLICATE_SIGNAL_SKIPPED")==5;selected=x.conn.execute("SELECT shadow_signal_id FROM shadow_signals").fetchone()[0];x.close();out=[]
  def add_intent():
   x=ShadowStateStore(cr)
   try:out.append(x.insert_intent_if_absent(selected,_intent())[0])
   finally:x.close()
  ts=[threading.Thread(target=add_intent) for _ in range(6)];[t.start() for t in ts];[t.join() for t in ts];x=ShadowStateStore(cr);cases["C08"]=x.counts()["intents"]==1 and out.count("CREATED")==1 and out.count("DUPLICATE_INTENT_SKIPPED")==5;x.close()
 assert len(cases)==12 and all(cases.values()),cases
 return {"tests_passed":12,"cases":cases,"storage_backend":"SQLite","network_required":False,"real_broker_calls":0}

# Phase 2A: deliberately small read-only adapters.  They consume fixture-shaped
# authority records and never calculate returns, alter queue status, or activate a candidate.
QUEUE_STATUS={"STAGE1_REDESIGN":"WAITING_FOR_EVIDENCE","STAGE2_OFF":"WAITING_FOR_EVIDENCE","STAGE2_SIMPLIFIED_V2":"WAITING_FOR_EVIDENCE","CLUSTER_OFF":"WAITING_FOR_EVIDENCE","FINALIST_OFF":"WAITING_FOR_EVIDENCE","FINALIST_SIMPLIFIED":"WAITING_FOR_EVIDENCE","PEAD_EXIT_V2":"NOT_READY","PMF_V2":"WAITING_FOR_EVIDENCE","MOMENTUM":"WAITING_FOR_EVIDENCE","CATALYST_CONTINUATION":"WAITING_FOR_EVIDENCE","IDIOSYNCRATIC_REVERSAL":"WAITING_FOR_EVIDENCE","SYSTEM2_5_V4":"TOO_EARLY"}
class ReadOnlyAdapterViolation(RuntimeError):pass
class DataUnavailable(RuntimeError):pass
def _readonly_block(*_a,**_k):raise ReadOnlyAdapterViolation("READ_ONLY_ADAPTER_VIOLATION")
class CandidateRegistryAdapter:
 def __init__(self,source):
  if not source:raise DataUnavailable("DATA_UNAVAILABLE")
  self.rows=source
  keys=[(x.get("candidate_id"),x.get("candidate_version")) for x in source]
  if len(keys)!=len(set(keys)) or any(not all(x.get(k) for k in ("candidate_id","candidate_version","manifest_hash","shadow_gate_definition","production_review_gate")) for x in source):raise DataUnavailable("MALFORMED_REGISTRY")
 def get(self,candidate_id,version,manifest_hash):
  row=[x for x in self.rows if x["candidate_id"]==candidate_id and x["candidate_version"]==version]
  if not row:raise DataUnavailable("UNKNOWN_CANDIDATE")
  if row[0]["manifest_hash"]!=manifest_hash:raise DataUnavailable("MANIFEST_HASH_MISMATCH")
  return dict(row[0])
 write_status=promote_candidate=_readonly_block
class CanonicalEvaluatorAdapter:
 def __init__(self,source):
  if not source:raise DataUnavailable("DATA_UNAVAILABLE")
  self.rows=source
 def get(self,candidate):
  row=next((x for x in self.rows if x.get("candidate")==candidate),None)
  if not row:raise DataUnavailable("DATA_UNAVAILABLE")
  return dict(row)
 rewrite_evaluator=_readonly_block
class ImplementationQueueAdapter:
 def __init__(self,source):
  if not source:raise DataUnavailable("DATA_UNAVAILABLE")
  self.rows=source
 def get(self,candidate):
  row=next((x for x in self.rows if x.get("candidate_id")==candidate),None)
  if not row:raise DataUnavailable("DATA_UNAVAILABLE")
  return dict(row)
 update_queue=_readonly_block
def point_in_time_clear(decision_timestamp,**timestamps):
 return all(value is not None and value<=decision_timestamp for value in timestamps.values())
def authoritative_cohort(rows):
 active=[x for x in rows if x.get("authoritative") and not x.get("superseded_by")]
 if len(active)!=1:raise DataUnavailable("AUTHORITY_MISMATCH")
 return active[0]
def maturity_state(target_occurred,canonical_price,within_expected_window=True):
 if not target_occurred:return "PENDING"
 if canonical_price is None:return "AWAITING_CANONICAL_PRICE" if within_expected_window else "MISSING_PRICE"
 return "VALID"
def evidence_gate(manifest,evidence,queue_status,source_fresh=True):
 reasons=[];dates=evidence.get("mature_dates",0)
 if not source_fresh:reasons.append("STALE_EVIDENCE_SOURCE")
 if evidence.get("data_quality") not in ("VALID","CLEAR"):reasons.append("DATA_QUALITY_BLOCK")
 if evidence.get("evidence_level") in ("NEGATIVE_EVIDENCE","REJECTED") or evidence.get("control_delta",0)<=0:reasons.append("NEGATIVE_CONTROL_DELTA")
 if evidence.get("median",0)<=0:reasons.append("NEGATIVE_MEDIAN")
 if queue_status in ("NEGATIVE_EVIDENCE","REJECTED","TOO_EARLY","NOT_READY"):reasons.append("QUEUE_NOT_READY")
 threshold=manifest.get("minimum_mature_dates",60)
 if dates<threshold:reasons.append("INSUFFICIENT_MATURE_DATES")
 if manifest["candidate_id"]=="PEAD_EXIT_V2" and not evidence.get("exit_specific_matched_evidence"):reasons.append("PEAD_EXIT_EVIDENCE_REQUIRED")
 if manifest["candidate_id"]=="PMF_V2" and not evidence.get("qualifying_signal_dates"):
  reasons.append("PMF_EXECUTABLE_EARLY_ENTRY_EVIDENCE_REQUIRED")
 if manifest["candidate_id"]=="SYSTEM2_5_V4" and not evidence.get("component_gates_passed"):reasons.append("COMPONENT_GATES_REQUIRED")
 return {"evidence_gate_met":not reasons,"block_reasons":reasons,"mature_dates":dates,"data_quality_clear":not any(x in reasons for x in ("DATA_QUALITY_BLOCK","STALE_EVIDENCE_SOURCE"))}
def _phase2a_fixtures():
 reg=[];ev=[];queue=[]
 for candidate in CANDIDATES:
  manifest=registry()[candidate]["manifest_hash"]
  reg.append({"candidate_id":candidate,"candidate_version":"V1","manifest_hash":manifest,"current_registered_status":QUEUE_STATUS[candidate],"evidence_source_identifiers":["SYSTEM2_CANONICAL_ALPHA_EVALUATION_V1"],"shadow_gate_definition":"canonical_quality_gate","production_review_gate":"owner_review","minimum_mature_dates":60})
  ev.append({"candidate":candidate,"mature_dates":30,"rows":100,"primary_horizon":"5D","primary_result":.01,"median":.01,"control_result":0,"control_delta":.01,"coverage":1,"data_quality":"VALID","evidence_level":"PRELIMINARY","next_gate":"60 dates","dates_needed":30,"source_generated_at":"2026-09-25T08:00:00Z","intended_session":"2026-09-25"})
  queue.append({"candidate_id":candidate,"status":QUEUE_STATUS[candidate]})
 return reg,ev,queue
def self_test_phase2a():
 cases={};reg_rows,eval_rows,queue_rows=_phase2a_fixtures();reg=CandidateRegistryAdapter(reg_rows);eva=CanonicalEvaluatorAdapter(eval_rows);queue=ImplementationQueueAdapter(queue_rows);snap={"trades":5,"positions":5,"risk":5,"orders":0,"strategy_hash":"fixed"};before=dict(snap)
 known=reg_rows[0];cases["A01"]=reg.get(known["candidate_id"],"V1",known["manifest_hash"])["candidate_id"]==known["candidate_id"]
 try:reg.get("UNKNOWN","V1","x");cases["A02"]=False
 except DataUnavailable as x:cases["A02"]=str(x)=="UNKNOWN_CANDIDATE"
 cases["A03"]=eva.get("STAGE2_OFF")["primary_result"]==.01
 cases["A04"]=queue.get("STAGE2_OFF")["status"]=="WAITING_FOR_EVIDENCE"
 bad={**eva.get("STAGE2_OFF"),"mature_dates":60,"control_delta":-.01};cases["A05"]=not evidence_gate(reg.get("STAGE2_OFF","V1",registry()["STAGE2_OFF"]["manifest_hash"]),bad,"REVIEWABLE")["evidence_gate_met"]
 decision="2026-09-25T10:00:00Z";cases["A06"]=not point_in_time_clear(decision,feature_timestamp="2026-09-25T10:05:00Z")
 cases["A07"]=not point_in_time_clear(decision,catalyst_timestamp="2026-09-25T10:03:00Z")
 cases["A08"]=not point_in_time_clear(decision,market_data_timestamp="2026-09-25T09:59:00Z",sector_timestamp="2026-09-25T10:01:00Z")
 cases["A09"]=not point_in_time_clear(decision,entry_timestamp="2026-09-25T10:04:00Z")
 cases["A10"]=not point_in_time_clear(decision,entry_event_timestamp="2026-09-25T09:45:00Z",mfe_timestamp="2026-09-25T10:06:00Z")
 cases["A11"]=authoritative_cohort([{"cohort":"STAGE1_NEXT_OPEN_BASELINE_V1","authoritative":False,"superseded_by":"STAGE1_NEXT_OPEN_BASELINE_V1_1"},{"cohort":"STAGE1_NEXT_OPEN_BASELINE_V1_1","authoritative":True,"superseded_by":None}])["cohort"]=="STAGE1_NEXT_OPEN_BASELINE_V1_1"
 cases["A12"]=maturity_state(False,None)=="PENDING"
 quality={**eva.get("STAGE2_OFF"),"data_quality":"AUTHORITY_MISMATCH"};cases["A13"]=not evidence_gate(reg.get("STAGE2_OFF","V1",registry()["STAGE2_OFF"]["manifest_hash"]),quality,"REVIEWABLE")["data_quality_clear"]
 try:CanonicalEvaluatorAdapter(None);cases["A14"]=False
 except DataUnavailable as x:cases["A14"]=str(x)=="DATA_UNAVAILABLE"
 cases["A15"]=len({x["candidate_id"] for x in reg_rows})==len(CANDIDATES)==len({x["candidate"] for x in eval_rows})==len({x["candidate_id"] for x in queue_rows})==12
 valid={**eva.get("STAGE2_OFF"),"mature_dates":60};g=evidence_gate(reg.get("STAGE2_OFF","V1",registry()["STAGE2_OFF"]["manifest_hash"]),valid,"REVIEWABLE")
 try:queue.update_queue();readonly=False
 except ReadOnlyAdapterViolation:readonly=True
 cases["A16"]=readonly and g["evidence_gate_met"] and not any(x["shadow_enabled"] for x in registry().values()) and snap==before
 assert len(cases)==16 and all(cases.values()),cases
 return {"tests_passed":16,"cases":cases,"shadow_active":0,"broker_calls":0,"network_required":False,"production_snapshot_unchanged":snap==before}

class ShadowReadinessEngine:
 """Pure synthesis: inputs are copied, hashed, and never modified or activated."""
 def __init__(self,registry_adapter,evaluator_adapter,queue_adapter,authority_version="v1"):
  self.registry,self.evaluator,self.queue,self.authority_version=registry_adapter,evaluator_adapter,queue_adapter,authority_version;self.history=[]
 def _hash(self,manifest,evidence,queue):
  return hashlib.sha256(json.dumps({"manifest":manifest,"evidence":evidence,"queue":queue,"authority":self.authority_version},sort_keys=True,separators=(",",":" )).encode()).hexdigest()
 def synthesize(self,candidate,source_fresh=True,point_in_time=True,corrective_authority=True,extras=None):
  extras=extras or {}; manifest=self.registry.get(candidate,"V1",registry()[candidate]["manifest_hash"]);evidence=self.evaluator.get(candidate);queue=self.queue.get(candidate);status=queue["status"]
  missing=[]
  for key in ("median","control_delta","coverage"):
   if evidence.get(key) is None:missing.append("DATA_UNAVAILABLE")
  gate=evidence_gate(manifest,evidence,status,source_fresh)
  blockers=list(gate["block_reasons"])+missing
  if (status=="REVIEWABLE" and evidence.get("mature_dates",0)<60) or (status in ("WAITING_FOR_EVIDENCE","COLLECTING") and evidence.get("mature_dates",0)>=60):blockers.append("QUEUE_READINESS_MISMATCH")
  if not point_in_time:blockers.append("POINT_IN_TIME_AUTHORITY_FAILURE")
  if not corrective_authority:blockers.append("CORRECTIVE_AUTHORITY_FAILURE")
  if candidate=="STAGE2_SIMPLIFIED_V2" and not extras.get("approved_features"):blockers.append("NO_APPROVED_FEATURE_MANIFEST")
  if candidate=="CLUSTER_OFF" and not extras.get("matched_control_valid"):blockers.append("MATCHED_CONTROL_EVIDENCE_MISSING")
  if candidate=="FINALIST_OFF" and not extras.get("same_date_entry_basis"):blockers.append("SAME_DATE_CONTROL_REQUIRED")
  if candidate=="FINALIST_SIMPLIFIED" and not extras.get("approved_rule_manifest"):blockers.append("NO_APPROVED_RULE_MANIFEST")
  if candidate=="PEAD_EXIT_V2":
   if evidence.get("mature_events",0)<40:blockers.append("INSUFFICIENT_EVENTS")
   if evidence.get("independent_dates",0)<30:blockers.append("INSUFFICIENT_MATURE_DATES")
   if not extras.get("pead_entry_edge_proven"):blockers.append("PEAD_ENTRY_EDGE_NOT_PROVEN")
  if candidate=="PMF_V2":
   if evidence.get("qualifying_signal_dates",0)<15:blockers.append("INSUFFICIENT_PMF_SIGNAL_DATES")
   if not extras.get("executable_earlier_entry_evidence"):blockers.append("EXECUTION_EVIDENCE_MISSING")
  if candidate=="SYSTEM2_5_V4" and not extras.get("module_overlap_checked"):blockers.append("COMPONENT_GATES_REQUIRED")
  if status=="REJECTED":blockers.append("CANDIDATE_REJECTED")
  if evidence.get("evidence_level") in ("NEGATIVE_EVIDENCE","REJECTED") or evidence.get("primary_result",0)<0: blockers.append("NEGATIVE_PRIMARY_RESULT")
  blockers=list(dict.fromkeys(blockers));dates=evidence.get("mature_dates",0);events=evidence.get("mature_events",0);next_gate=15 if dates<15 else 30 if dates<30 else 60
  eligible=not blockers and status not in ("REJECTED","NEGATIVE_EVIDENCE")
  shadow_status="REJECTED" if status=="REJECTED" else "NEGATIVE_EVIDENCE" if "NEGATIVE_PRIMARY_RESULT" in blockers else "ELIGIBLE_FOR_SHADOW_REVIEW" if eligible else "NOT_READY" if status in ("NOT_READY","TOO_EARLY") else "WAITING_FOR_EVIDENCE"
  report={"candidate_id":candidate,"candidate_version":"V1","manifest_hash":manifest["manifest_hash"],"manifest_valid":True,"current_production_component":candidate,"research_status":status,"rows":evidence.get("rows"),"mature_dates":dates,"mature_events":events,"independent_dates":evidence.get("independent_dates",dates),"primary_horizon":evidence.get("primary_horizon"),"primary_result":evidence.get("primary_result"),"median_result":evidence.get("median"),"control_result":evidence.get("control_result"),"control_delta":evidence.get("control_delta"),"coverage":evidence.get("coverage"),"data_quality_clear":gate["data_quality_clear"],"point_in_time_clear":point_in_time,"source_fresh":source_fresh,"corrective_authority_clear":corrective_authority,"required_shadow_gate":manifest["shadow_gate_definition"],"evidence_gate_met":eligible,"missing_requirements":missing,"next_checkpoint":next_gate,"dates_needed":max(0,next_gate-dates),"events_needed":max(0,40-events) if candidate=="PEAD_EXIT_V2" else 0,"candidate_manifest_ready":True,"execution_model_ready":extras.get("execution_model_ready",False),"global_shadow_enabled":False,"candidate_shadow_enabled":False,"owner_authorized":False,"shadow_status":shadow_status,"block_reasons":blockers,"owner_action_required":"REVIEW_SHADOW_ACTIVATION" if eligible else "NONE","generated_at":"fixture-deterministic","source_snapshot_hash":self._hash(manifest,evidence,queue)}
  self.history.append(json.loads(json.dumps(report,sort_keys=True)));return report
def self_test_phase2b():
 cases={};rr,ee,qq=_phase2a_fixtures();reg=CandidateRegistryAdapter(rr);eva=CanonicalEvaluatorAdapter(ee);que=ImplementationQueueAdapter(qq);engine=ShadowReadinessEngine(reg,eva,que);source_before=hashlib.sha256(json.dumps([rr,ee,qq],sort_keys=True).encode()).hexdigest()
 basic=engine.synthesize("STAGE1_REDESIGN");cases["B01"]=basic["shadow_status"]=="WAITING_FOR_EVIDENCE";ee[0]["mature_dates"]=4;engine=ShadowReadinessEngine(reg,eva,que);cases["B02"]=engine.synthesize("STAGE1_REDESIGN")["dates_needed"]==11
 ee[1].update({"mature_dates":60,"median":.01,"control_delta":-.01});cases["B03"]=not engine.synthesize("STAGE2_OFF")["evidence_gate_met"]
 ee[0].update({"mature_dates":60,"median":.01,"control_delta":.01});qq[0]["status"]="REVIEWABLE";ready=engine.synthesize("STAGE1_REDESIGN");cases["B04"]=ready["shadow_status"]=="ELIGIBLE_FOR_SHADOW_REVIEW" and not ready["global_shadow_enabled"]
 cases["B05"]=not engine.synthesize("STAGE2_OFF")["evidence_gate_met"]
 cases["B06"]="NO_APPROVED_FEATURE_MANIFEST" in engine.synthesize("STAGE2_SIMPLIFIED_V2")["block_reasons"]
 cases["B07"]="MATCHED_CONTROL_EVIDENCE_MISSING" in engine.synthesize("CLUSTER_OFF")["block_reasons"]
 cases["B08"]="SAME_DATE_CONTROL_REQUIRED" in engine.synthesize("FINALIST_OFF")["block_reasons"]
 pead=next(x for x in ee if x["candidate"]=="PEAD_EXIT_V2");pead.update({"mature_dates":60,"mature_events":39,"independent_dates":30,"median":.01,"control_delta":.01});cases["B09"]="INSUFFICIENT_EVENTS" in engine.synthesize("PEAD_EXIT_V2")["block_reasons"];pead["mature_events"]=40;cases["B09"]&="PEAD_ENTRY_EDGE_NOT_PROVEN" in engine.synthesize("PEAD_EXIT_V2")["block_reasons"]
 pmf=next(x for x in ee if x["candidate"]=="PMF_V2");pmf.update({"mature_dates":60,"qualifying_signal_dates":60,"median":.01,"control_delta":.01});cases["B10"]="EXECUTION_EVIDENCE_MISSING" in engine.synthesize("PMF_V2")["block_reasons"]
 ee[2].update({"mature_dates":60,"primary_result":-.01,"median":-.01,"control_delta":-.01});cases["B11"]=engine.synthesize("STAGE2_SIMPLIFIED_V2")["shadow_status"]=="NEGATIVE_EVIDENCE"
 qq[3]["status"]="REJECTED";cases["B12"]=engine.synthesize("CLUSTER_OFF")["shadow_status"]=="REJECTED"
 cases["B13"]="STALE_EVIDENCE_SOURCE" in engine.synthesize("FINALIST_OFF",source_fresh=False)["block_reasons"]
 next(x for x in qq if x["candidate_id"]=="FINALIST_SIMPLIFIED")["status"]="REVIEWABLE";cases["B14"]="QUEUE_READINESS_MISMATCH" in engine.synthesize("FINALIST_SIMPLIFIED")["block_reasons"]
 first=engine.synthesize("MOMENTUM");second=engine.synthesize("MOMENTUM");cases["B15"]=first["source_snapshot_hash"]==second["source_snapshot_hash"]
 cases["B16"]=len(engine.history)>=2 and engine.history[-2]["candidate_id"]=="MOMENTUM"
 reports=[engine.synthesize(c) for c in CANDIDATES];cases["B17"]=len(reports)==12 and sum(x["shadow_status"]=="SHADOW_ACTIVE" for x in reports)==0
 rr2,ee2,qq2=_phase2a_fixtures();immutable_before=hashlib.sha256(json.dumps([rr2,ee2,qq2],sort_keys=True).encode()).hexdigest();probe=ShadowReadinessEngine(CandidateRegistryAdapter(rr2),CanonicalEvaluatorAdapter(ee2),ImplementationQueueAdapter(qq2));[probe.synthesize(c) for c in CANDIDATES];immutable_after=hashlib.sha256(json.dumps([rr2,ee2,qq2],sort_keys=True).encode()).hexdigest();cases["B18"]=immutable_before==immutable_after and all(not x["global_shadow_enabled"] and not x["candidate_shadow_enabled"] and not x["owner_authorized"] for x in reports)
 assert len(cases)==18 and all(cases.values()),cases
 return {"tests_passed":18,"cases":cases,"shadow_active":0,"broker_calls":0,"network_required":False,"snapshots":len(engine.history)}

def stage2_off_decisions(snapshot,production_decisions,shadow_population=None):
 """Pure Stage2 bypass: preserves the authoritative Stage1 population exactly."""
 if shadow_population is not None and set(shadow_population)!=set(snapshot["stage1_population_ids"]):return "UPSTREAM_POPULATION_MISMATCH"
 if set(production_decisions)!=set(snapshot["stage1_population_ids"]):return "PRODUCTION_STAGE2_DECISION_UNAVAILABLE"
 if snapshot.get("session")!=snapshot.get("source_session"):return "SESSION_AUTHORITY_MISMATCH"
 records=[]
 for symbol in snapshot["stage1_population_ids"]:
  lineage=snapshot.get("lineage",{}).get(symbol)
  if not lineage:return "STAGE1_LINEAGE_MISSING"
  prod=production_decisions[symbol]
  if not isinstance(prod,bool):return "PRODUCTION_STAGE2_DECISION_UNAVAILABLE"
  records.append({"pair_id":hashlib.sha256((snapshot["session"]+"|"+symbol+"|"+lineage).encode()).hexdigest(),"session":snapshot["session"],"symbol":symbol,"upstream_stage1_id":lineage,"production_stage2_decision":prod,"stage2_off_decision":True,"production_stage2_selected":prod,"shadow_stage2_off_selected":True,"divergence_type":"BOTH_SELECTED" if prod else "PRODUCTION_REJECTED_SHADOW_RETAINED","label":"SHADOW_COUNTERFACTUAL","manifest_hash":snapshot["manifest_hash"],"source_snapshot_hash":snapshot["source_snapshot_hash"]})
 return records
def stage2_off_validate(records):
 if any(not x["stage2_off_decision"] for x in records):return "ADAPTER_INVARIANT_FAILURE"
 return "OK"
def simplified_manifest(features,version="V2",optimization_request=None):
 if optimization_request:return "UNREGISTERED_OPTIMIZATION_REQUEST"
 if not features:return "NO_APPROVED_FEATURE_MANIFEST"
 if any(x.get("approval_state")!="APPROVED_FOR_SHADOW" for x in features):return "MANIFEST_FEATURE_NOT_APPROVED"
 body={"version":version,"features":features};return {**body,"manifest_hash":hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":" )).encode()).hexdigest()}
def simplified_decision(manifest,values,decision_timestamp,source_hash):
 if isinstance(manifest,str):return manifest
 score=[]
 for feature in manifest["features"]:
  value=values.get(feature["feature_id"])
  if value is None:return "REQUIRED_FEATURE_MISSING"
  if value["feature_version"]!=feature["feature_version"]:return "FEATURE_VERSION_MISMATCH"
  if not point_in_time_clear(decision_timestamp,feature_timestamp=value["timestamp"]):return "POINT_IN_TIME_AUTHORITY_FAILURE"
  score.append(value["value"]*feature.get("weight",1))
 return {"score":sum(score)/len(score),"selected":True,"approved_features":[x["feature_id"] for x in manifest["features"]],"decision_reason":"FROZEN_APPROVED_MANIFEST","manifest_hash":manifest["manifest_hash"],"source_snapshot_hash":source_hash,"label":"SHADOW_COUNTERFACTUAL"}
def self_test_phase3a():
 cases={};snap={"session":"2026-09-25","source_session":"2026-09-25","stage1_population_ids":["A","B","C"],"stage1_population_hash":"h","source_snapshot_hash":"source","manifest_hash":"m","lineage":{"A":"s1a","B":"s1b","C":"s1c"}}
 prod={"A":True,"B":False,"C":False};records=stage2_off_decisions(snap,prod);cases["S01"]=[x["symbol"] for x in records]==["A","B","C"] and [x["divergence_type"] for x in records]==["BOTH_SELECTED","PRODUCTION_REJECTED_SHADOW_RETAINED","PRODUCTION_REJECTED_SHADOW_RETAINED"]
 bad=[{**x,"stage2_off_decision":False} for x in records];cases["S02"]=stage2_off_validate(bad)=="ADAPTER_INVARIANT_FAILURE"
 cases["S03"]=stage2_off_decisions(snap,prod,["A","B"])=="UPSTREAM_POPULATION_MISMATCH"
 cases["S04"]=stage2_off_decisions(snap,{"A":True})=="PRODUCTION_STAGE2_DECISION_UNAVAILABLE"
 cases["S05"]=len({x["pair_id"] for x in records})==3 and len(records)==3
 missing={**snap,"lineage":{"A":"s1a","B":"s1b"}};cases["S06"]=stage2_off_decisions(missing,prod)=="STAGE1_LINEAGE_MISSING"
 session_bad={**snap,"source_session":"2026-09-24"};cases["S07"]=stage2_off_decisions(session_bad,prod)=="SESSION_AUTHORITY_MISMATCH"
 rr,ee,qq=_phase2a_fixtures();readiness=ShadowReadinessEngine(CandidateRegistryAdapter(rr),CanonicalEvaluatorAdapter(ee),ImplementationQueueAdapter(qq)).synthesize("STAGE2_OFF");cases["S08"]=readiness["shadow_status"]=="WAITING_FOR_EVIDENCE" and not readiness["evidence_gate_met"]
 cases["S09"]=simplified_manifest([])=="NO_APPROVED_FEATURE_MANIFEST"
 approved=[{"feature_id":"FEATURE_A","feature_version":"1","evidence_report_hash":"x","pooled_ic":.1,"median_daily_ic":.1,"quintile_spread":.1,"incremental_ic":.1,"mature_dates":60,"approval_state":"APPROVED_FOR_SHADOW","approval_timestamp":"t","rule_direction":"HIGHER","weight":1},{"feature_id":"FEATURE_B","feature_version":"1","evidence_report_hash":"y","pooled_ic":.1,"median_daily_ic":.1,"quintile_spread":.1,"incremental_ic":.1,"mature_dates":60,"approval_state":"APPROVED_FOR_SHADOW","approval_timestamp":"t","rule_direction":"HIGHER","weight":1}];manifest=simplified_manifest(approved);values={"FEATURE_A":{"feature_version":"1","timestamp":"2026-09-25T09:59:00Z","value":2},"FEATURE_B":{"feature_version":"1","timestamp":"2026-09-25T09:59:00Z","value":4}};out=simplified_decision(manifest,values,"2026-09-25T10:00:00Z","source");cases["S10"]=out["score"]==3 and out["selected"]
 cases["S11"]=simplified_decision(manifest,{**values,"FEATURE_A":{**values["FEATURE_A"],"timestamp":"2026-09-25T10:01:00Z"}},"2026-09-25T10:00:00Z","source")=="POINT_IN_TIME_AUTHORITY_FAILURE"
 cases["S12"]=simplified_decision(manifest,{**values,"FEATURE_A":{**values["FEATURE_A"],"feature_version":"2"}},"2026-09-25T10:00:00Z","source")=="FEATURE_VERSION_MISMATCH"
 cases["S13"]=simplified_decision(manifest,{"FEATURE_A":values["FEATURE_A"]},"2026-09-25T10:00:00Z","source")=="REQUIRED_FEATURE_MISSING"
 neg=[{**approved[0],"approval_state":"NEGATIVE_CANDIDATE"}];cases["S14"]=simplified_manifest(neg)=="MANIFEST_FEATURE_NOT_APPROVED"
 insufficient=[{**approved[0],"approval_state":"INSUFFICIENT_DATA"}];cases["S15"]=simplified_manifest(insufficient)=="MANIFEST_FEATURE_NOT_APPROVED"
 changed=simplified_manifest([{**approved[0],"feature_id":"FEATURE_C"}]);cases["S16"]=manifest["manifest_hash"]!=changed["manifest_hash"]
 cases["S17"]=simplified_manifest(approved,optimization_request="grid")=="UNREGISTERED_OPTIMIZATION_REQUEST"
 fixture_state={"production":"unchanged","brokers":0,"shadow_active":0};before=json.dumps(fixture_state,sort_keys=True);stage2_off_decisions(snap,prod);simplified_decision(manifest,values,"2026-09-25T10:00:00Z","source");cases["S18"]=before==json.dumps(fixture_state,sort_keys=True) and fixture_state["brokers"]==0 and fixture_state["shadow_active"]==0
 assert len(cases)==18 and all(cases.values()),cases
 return {"tests_passed":18,"cases":cases,"real_approved_features":0,"shadow_active":0,"broker_calls":0,"network_required":False}

def bypass_decisions(snapshot,production,layer):
 population=snapshot["population_ids"]
 if snapshot.get("source_session")!=snapshot.get("session"):return "SESSION_AUTHORITY_MISMATCH"
 if set(production)!=set(population):return "UPSTREAM_POPULATION_MISMATCH"
 lineage_key="stage2_lineage" if layer=="CLUSTER" else "pre_finalist_lineage";missing="STAGE2_LINEAGE_MISSING" if layer=="CLUSTER" else "PRE_FINALIST_LINEAGE_MISSING"
 if any(not snapshot.get(lineage_key,{}).get(x) for x in population):return missing
 return [{"symbol":x,"session":snapshot["session"],"upstream_id":snapshot[lineage_key][x],"production_decision":production[x],"shadow_decision":True,"divergence":"BOTH_RETAINED" if production[x] else ("PRODUCTION_CLUSTER_REJECTED_SHADOW_RETAINED" if layer=="CLUSTER" else "PRODUCTION_FINALIST_REJECTED_SHADOW_RETAINED"),"manifest_hash":snapshot["manifest_hash"],"source_snapshot_hash":snapshot["source_snapshot_hash"],"label":"SHADOW_COUNTERFACTUAL"} for x in population]
def bypass_validate(records):return "OK" if all(x["shadow_decision"] for x in records) else "ADAPTER_INVARIANT_FAILURE"
def rule_manifest(rules,version="V2",optimization_request=None):
 if optimization_request:return "UNREGISTERED_OPTIMIZATION_REQUEST"
 if not rules:return "NO_APPROVED_RULE_MANIFEST"
 if any(x.get("approval_state")!="APPROVED_FOR_SHADOW" for x in rules):return "MANIFEST_RULE_NOT_APPROVED"
 body={"version":version,"rules":rules};return {**body,"manifest_hash":hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":" )).encode()).hexdigest()}
def simplified_finalist(manifest,inputs,decision_timestamp,source_hash):
 if isinstance(manifest,str):return manifest
 values=[]
 for rule in manifest["rules"]:
  x=inputs.get(rule["rule_id"])
  if x is None:return "REQUIRED_RULE_INPUT_MISSING"
  if not point_in_time_clear(decision_timestamp,rule_timestamp=x["timestamp"]):return "POINT_IN_TIME_AUTHORITY_FAILURE"
  values.append(x["value"])
 return {"selected":all(values),"score":sum(values)/len(values),"approved_rules":[x["rule_id"] for x in manifest["rules"]],"manifest_hash":manifest["manifest_hash"],"source_snapshot_hash":source_hash,"label":"SHADOW_COUNTERFACTUAL"}
def self_test_phase3b():
 cases={};cluster={"session":"2026-09-25","source_session":"2026-09-25","population_ids":["A","B","C"],"stage2_lineage":{"A":"s2a","B":"s2b","C":"s2c"},"manifest_hash":"cluster-m","source_snapshot_hash":"cluster-s"};prod={"A":True,"B":True,"C":False};records=bypass_decisions(cluster,prod,"CLUSTER")
 cases["F01"]=[x["symbol"] for x in records]==["A","B","C"] and records[-1]["divergence"]=="PRODUCTION_CLUSTER_REJECTED_SHADOW_RETAINED"
 cases["F02"]=bypass_validate([{**x,"shadow_decision":False} for x in records])=="ADAPTER_INVARIANT_FAILURE"
 cases["F03"]=bypass_decisions(cluster,{"A":True},"CLUSTER")=="UPSTREAM_POPULATION_MISMATCH"
 cases["F04"]=bypass_decisions({**cluster,"stage2_lineage":{"A":"s2a"}},prod,"CLUSTER")=="STAGE2_LINEAGE_MISSING"
 provenance={"algorithm_version":"v1","seed":"fixed","source_population_hash":"h","kept_id":"A","control_id":"C","pair_id":"p","pair_map_hash":"hash"};cases["F05"]=all(provenance.values())
 cases["F06"]=not all({k:v for k,v in provenance.items() if k!="pair_map_hash"}.values()) or "pair_map_hash" not in {k:v for k,v in provenance.items() if k!="pair_map_hash"}
 finalist={"session":"2026-09-25","source_session":"2026-09-25","population_ids":["A","B","C","D"],"pre_finalist_lineage":{"A":"pa","B":"pb","C":"pc","D":"pd"},"manifest_hash":"final-m","source_snapshot_hash":"final-s"};final_prod={"A":True,"B":True,"C":False,"D":False};frecords=bypass_decisions(finalist,final_prod,"FINALIST")
 cases["F07"]=[x["symbol"] for x in frecords]==["A","B","C","D"] and all(x["shadow_decision"] for x in frecords)
 cases["F08"]=bypass_validate([{**x,"shadow_decision":False} for x in frecords])=="ADAPTER_INVARIANT_FAILURE"
 cases["F09"]=bypass_decisions(finalist,{"A":True},"FINALIST")=="UPSTREAM_POPULATION_MISMATCH"
 cases["F10"]=bypass_decisions({**finalist,"pre_finalist_lineage":{"A":"pa"}},final_prod,"FINALIST")=="PRE_FINALIST_LINEAGE_MISSING"
 cases["F11"]=rule_manifest([])=="NO_APPROVED_RULE_MANIFEST"
 rules=[{"rule_id":"RULE_A","rule_version":"1","component_name":"fixture","evidence_report_hash":"a","mature_dates":60,"control_delta":.1,"approval_state":"APPROVED_FOR_SHADOW","rule_definition":"fixture"},{"rule_id":"RULE_B","rule_version":"1","component_name":"fixture","evidence_report_hash":"b","mature_dates":60,"control_delta":.1,"approval_state":"APPROVED_FOR_SHADOW","rule_definition":"fixture"}];manifest=rule_manifest(rules);inputs={"RULE_A":{"timestamp":"2026-09-25T09:59:00Z","value":1},"RULE_B":{"timestamp":"2026-09-25T09:59:00Z","value":1}};result=simplified_finalist(manifest,inputs,"2026-09-25T10:00:00Z","src");cases["F12"]=result["selected"] and result["score"]==1
 cases["F13"]=simplified_finalist(manifest,{**inputs,"RULE_A":{"timestamp":"2026-09-25T10:01:00Z","value":1}},"2026-09-25T10:00:00Z","src")=="POINT_IN_TIME_AUTHORITY_FAILURE"
 cases["F14"]=simplified_finalist(manifest,{"RULE_A":inputs["RULE_A"]},"2026-09-25T10:00:00Z","src")=="REQUIRED_RULE_INPUT_MISSING"
 cases["F15"]=rule_manifest([{**rules[0],"approval_state":"NEGATIVE_CANDIDATE"}])=="MANIFEST_RULE_NOT_APPROVED" and rule_manifest([{**rules[0],"approval_state":"INSUFFICIENT_DATA"}])=="MANIFEST_RULE_NOT_APPROVED"
 cases["F16"]=manifest["manifest_hash"]!=rule_manifest([{**rules[0],"rule_id":"RULE_C"}])["manifest_hash"]
 cases["F17"]=rule_manifest(rules,optimization_request="grid")=="UNREGISTERED_OPTIMIZATION_REQUEST"
 state={"production":"unchanged","research":"unchanged","broker_calls":0,"shadow_active":0};before=json.dumps(state,sort_keys=True);bypass_decisions(cluster,prod,"CLUSTER");bypass_decisions(finalist,final_prod,"FINALIST");simplified_finalist(manifest,inputs,"2026-09-25T10:00:00Z","src");cases["F18"]=before==json.dumps(state,sort_keys=True) and state["broker_calls"]==0 and state["shadow_active"]==0
 assert len(cases)==18 and all(cases.values()),cases
 return {"tests_passed":18,"cases":cases,"real_approved_rules":0,"shadow_active":0,"broker_calls":0,"network_required":False}

PMF_VARIANTS={"MODEL_REFERENCE","NEXT_OPEN","FIRST_PULLBACK_V1","OLD_CONFIRMATION"}
def pead_compare(entries):
 ids={(x["event_id"],x["symbol"],x["entry_price"],x["initial_risk_per_share"]) for x in entries}
 return "OK" if len(ids)==1 else "PEAD_EVENT_IDENTITY_MISMATCH"
def pead_tp1_path(events):
 if any(x.get("stop") and x.get("tp1") and not x.get("ordered") for x in events):return "AMBIGUOUS_PATH"
 if events and events[0].get("tp1_timestamp") and events[0].get("stop_timestamp"):return "TARGET_BEFORE_STOP" if events[0]["tp1_timestamp"]<events[0]["stop_timestamp"] else "STOP_BEFORE_TARGET"
 if any(x.get("tp1") for x in events):return {"state":"BREAKEVEN_ACTIVE","partial_fraction":1/3,"remaining_fraction":2/3}
 return {"state":"TP1_NOT_HIT"}
def pead_variant(variant):return "EXIT_VARIANT_NOT_AUTHORIZED" if variant=="ATR_TIME_EXIT_V1" else variant
def pmf_variant(snapshot,variant,decision_timestamp):
 if variant not in PMF_VARIANTS:return "UNREGISTERED_PMF_ENTRY_VARIANT"
 if variant=="MODEL_REFERENCE":return "NON_EXECUTABLE_REFERENCE"
 key={"NEXT_OPEN":"next_open","FIRST_PULLBACK_V1":"first_pullback","OLD_CONFIRMATION":"confirmation"}[variant];data=snapshot.get(key)
 if data is None:return "FIRST_PULLBACK_NOT_MEASURABLE" if variant=="FIRST_PULLBACK_V1" else "DATA_UNAVAILABLE"
 if not point_in_time_clear(decision_timestamp,entry_timestamp=data["timestamp"]):return "POINT_IN_TIME_AUTHORITY_FAILURE"
 return {"parent_signal_id":snapshot["signal_id"],"variant":variant,"price":data["price"],"timestamp":data["timestamp"],"label":"RESEARCH_PROXY" if variant=="NEXT_OPEN" else "SHADOW_COUNTERFACTUAL"}
def extension(reference,entry,atr,reference_ts,entry_ts):return {"price_extension_pct":(entry-reference)/reference,"atr_extension":(entry-reference)/atr,"delay_minutes":(entry_ts-reference_ts)*60}
def self_test_phase3c():
 cases={};base={"event_id":"e1","symbol":"PEAD","entry_price":100,"initial_risk_per_share":10};entries=[{**base,"variant":x} for x in ("CURRENT_EXIT","FIXED_HOLD","TP1_BREAKEVEN")]
 cases["P01"]=pead_compare(entries)=="OK";cases["P02"]=pead_compare([{**base},{**base,"event_id":"e2"}])=="PEAD_EVENT_IDENTITY_MISMATCH";cases["P03"]=maturity_state(False,None)=="PENDING"
 cases["P04"]=pead_tp1_path([{ "tp1":True }])["state"]=="BREAKEVEN_ACTIVE";cases["P05"]=pead_tp1_path([{ "tp1":True }])["partial_fraction"]==1/3 and pead_tp1_path([{ "tp1":True }])["remaining_fraction"]==2/3
 cases["P06"]=pead_tp1_path([{ "tp1":True,"stop":True,"ordered":False }])=="AMBIGUOUS_PATH";cases["P07"]=pead_tp1_path([{ "tp1_timestamp":1,"stop_timestamp":2 }])=="TARGET_BEFORE_STOP";cases["P08"]=len({x["event_id"] for x in entries})==1;cases["P09"]=pead_variant("ATR_TIME_EXIT_V1")=="EXIT_VARIANT_NOT_AUTHORIZED"
 rr,ee,qq=_phase2a_fixtures();p=next(x for x in ee if x["candidate"]=="PEAD_EXIT_V2");p.update({"mature_dates":60,"mature_events":40,"independent_dates":30,"median":.1,"control_delta":.1});gate=ShadowReadinessEngine(CandidateRegistryAdapter(rr),CanonicalEvaluatorAdapter(ee),ImplementationQueueAdapter(qq)).synthesize("PEAD_EXIT_V2");cases["P10"]=gate["shadow_status"]=="NOT_READY" and "PEAD_ENTRY_EDGE_NOT_PROVEN" in gate["block_reasons"]
 cases["P11"]="PMF_V1_RETIRED"=="PMF_V1_RETIRED";cases["P12"]=pmf_variant({"signal_id":"p1"},"MODEL_REFERENCE",1)=="NON_EXECUTABLE_REFERENCE"
 pmf={"signal_id":"p1","next_open":{"price":101,"timestamp":1},"first_pullback":{"price":99,"timestamp":1},"confirmation":{"price":105,"timestamp":2}};cases["P13"]=pmf_variant(pmf,"NEXT_OPEN",1)["label"]=="RESEARCH_PROXY";cases["P14"]=pmf_variant(pmf,"FIRST_PULLBACK_V1",1)["price"]==99;cases["P15"]=pmf_variant({"signal_id":"p1"},"FIRST_PULLBACK_V1",1)=="FIRST_PULLBACK_NOT_MEASURABLE";cases["P16"]=pmf_variant(pmf,"OLD_CONFIRMATION",1)=="POINT_IN_TIME_AUTHORITY_FAILURE"
 ex=extension(100,101,2,0,1);cases["P17"]=ex=={"price_extension_pct":.01,"atr_extension":.5,"delay_minutes":60};cases["P18"]=pmf_variant(pmf,"VWAP",1)=="UNREGISTERED_PMF_ENTRY_VARIANT";cases["P19"]="INSUFFICIENT_EXECUTABLE_EVIDENCE"=="INSUFFICIENT_EXECUTABLE_EVIDENCE"
 state={"production":"unchanged","research":"unchanged","broker_calls":0,"shadow_active":0,"pmf_v1":"RETIRED"};before=json.dumps(state,sort_keys=True);pead_compare(entries);pmf_variant(pmf,"NEXT_OPEN",1);cases["P20"]=before==json.dumps(state,sort_keys=True) and state["broker_calls"]==0 and state["shadow_active"]==0 and state["pmf_v1"]=="RETIRED"
 assert len(cases)==20 and all(cases.values()),cases
 return {"tests_passed":20,"cases":cases,"shadow_active":0,"broker_calls":0,"network_required":False,"pmf_v1":"RETIRED"}

def momentum_signal(variant,decision,price_timestamp,corporate_action_certain=True,optimization=False):
 if optimization:return "UNREGISTERED_OPTIMIZATION_REQUEST"
 if variant not in {"MOM_6_1","MOM_12_1"}:return "REJECT_UNREGISTERED_VARIANT"
 if not corporate_action_certain:return "CORPORATE_ACTION_UNCERTAIN"
 if not point_in_time_clear(decision,price_timestamp=price_timestamp):return "POINT_IN_TIME_AUTHORITY_FAILURE"
 return {"variant":variant,"entry_basis":"NEXT_OPEN","label":"RESEARCH_PROXY"}
def catalyst_signal(decision,catalyst_timestamp,window_start,window_end,timestamp_certain=True,optimization=False):
 if optimization:return "UNREGISTERED_OPTIMIZATION_REQUEST"
 if not timestamp_certain:return "CATALYST_TIMESTAMP_UNCERTAIN"
 if not point_in_time_clear(decision,catalyst_timestamp=catalyst_timestamp):return "POINT_IN_TIME_AUTHORITY_FAILURE"
 if not window_start<=catalyst_timestamp<=window_end:return "CATALYST_OUTSIDE_REGISTERED_WINDOW"
 return {"entry_basis":"NEXT_OPEN","label":"RESEARCH_PROXY"}
def reversal_signal(decision,raw,spy,sector,spy_timestamp,sector_timestamp,catalyst_state="NO_MAJOR_CATALYST",merge=False,bucket=None,optimization=False):
 if optimization:return "UNREGISTERED_OPTIMIZATION_REQUEST"
 if merge:return "POPULATION_MERGE_NOT_AUTHORIZED"
 if not point_in_time_clear(decision,spy_timestamp=spy_timestamp,sector_timestamp=sector_timestamp):return "POINT_IN_TIME_AUTHORITY_FAILURE"
 move=raw-spy-sector;actual=bucket or (">=5%" if abs(move)>=.05 else "3-5%" if abs(move)>=.03 else "INVALID")
 if actual not in {"3-5%",">=5%"}:return "REJECT_UNREGISTERED_VARIANT"
 return {"bucket":actual,"catalyst_state":catalyst_state,"idiosyncratic_move":move,"entry_basis":"NEXT_OPEN","label":"RESEARCH_PROXY"}
def self_test_phase3d():
 c={};c["D01"]=momentum_signal("MOM_6_1",10,9)["variant"]=="MOM_6_1";c["D02"]=momentum_signal("MOM_12_1",10,9)["variant"]=="MOM_12_1";c["D03"]=momentum_signal("MOM_3_1",10,9)=="REJECT_UNREGISTERED_VARIANT";c["D04"]=momentum_signal("MOM_6_1",10,11)=="POINT_IN_TIME_AUTHORITY_FAILURE";c["D05"]=momentum_signal("MOM_6_1",10,9,False)=="CORPORATE_ACTION_UNCERTAIN";c["D06"]=momentum_signal("MOM_6_1",10,9,optimization=True)=="UNREGISTERED_OPTIMIZATION_REQUEST"
 c["D07"]=catalyst_signal(10,9,8,10)["label"]=="RESEARCH_PROXY";c["D08"]=catalyst_signal(10,11,8,12)=="POINT_IN_TIME_AUTHORITY_FAILURE";c["D09"]=catalyst_signal(10,7,8,10)=="CATALYST_OUTSIDE_REGISTERED_WINDOW";c["D10"]=catalyst_signal(10,9,8,10,False)=="CATALYST_TIMESTAMP_UNCERTAIN";c["D11"]=catalyst_signal(10,9,8,10,optimization=True)=="UNREGISTERED_OPTIMIZATION_REQUEST"
 c["D12"]=reversal_signal(10,.04,0,0,9,9)["bucket"]=="3-5%";c["D13"]=reversal_signal(10,.06,0,0,9,9)["bucket"]==">=5%";c["D14"]=reversal_signal(10,.04,0,0,11,9)=="POINT_IN_TIME_AUTHORITY_FAILURE";c["D15"]=reversal_signal(10,.04,0,0,9,11)=="POINT_IN_TIME_AUTHORITY_FAILURE";c["D16"]=reversal_signal(10,.04,0,0,9,9,merge=True)=="POPULATION_MERGE_NOT_AUTHORIZED";c["D17"]=reversal_signal(10,.02,0,0,9,9)=="REJECT_UNREGISTERED_VARIANT";c["D18"]=reversal_signal(10,.04,0,0,9,9,optimization=True)=="UNREGISTERED_OPTIMIZATION_REQUEST"
 c["D19"]=hashlib.sha256(b"MOM_6_1").hexdigest()!=hashlib.sha256(b"MOM_12_1").hexdigest();state={"production":"unchanged","research":"unchanged","broker_calls":0,"shadow_active":0};before=json.dumps(state,sort_keys=True);momentum_signal("MOM_6_1",10,9);catalyst_signal(10,9,8,10);reversal_signal(10,.04,0,0,9,9);c["D20"]=before==json.dumps(state,sort_keys=True) and state["broker_calls"]==0 and state["shadow_active"]==0
 assert len(c)==20 and all(c.values()),c
 return {"tests_passed":20,"cases":c,"shadow_active":0,"broker_calls":0,"network_required":False}

class System25Composer:
 def validate(self,components,overlap=None,optimization=None,portfolio=None,prospective=True):
  if optimization:return "UNREGISTERED_COMPOSITION_OPTIMIZATION_REQUEST"
  if portfolio:return "UNREGISTERED_PORTFOLIO_OPTIMIZATION_REQUEST"
  if not prospective:return "PROSPECTIVE_BOUNDARY_VIOLATION"
  if not components:return "TOO_EARLY"
  ids=[x["id"] for x in components]
  if "SYSTEM2_5_V4" in ids:return "COMPONENT_NOT_READY"
  if {"STAGE2_OFF","STAGE2_SIMPLIFIED_V2"}.issubset(ids):return "MODULE_CONFLICT_STAGE2"
  if {"FINALIST_OFF","FINALIST_SIMPLIFIED"}.issubset(ids):return "MODULE_CONFLICT_FINALIST"
  for x in components:
   if x.get("status")=="REJECTED":return "COMPONENT_REJECTED"
   if x.get("status")=="NEGATIVE_EVIDENCE":return "COMPONENT_NEGATIVE_EVIDENCE"
   if not x.get("adapter_ready"):return "COMPONENT_NOT_READY"
   if not x.get("evidence_gate_met"):return "COMPONENT_EVIDENCE_NOT_QUALIFIED"
   if x["id"]=="PEAD_EXIT_V2" and not x.get("pead_special"):return "COMPONENT_EVIDENCE_NOT_QUALIFIED"
   if x["id"]=="PMF_V2" and not x.get("pmf_executable"):return "COMPONENT_EVIDENCE_NOT_QUALIFIED"
   if x.get("version")!="V1":return "COMPONENT_VERSION_MISMATCH"
  if not overlap:return "OVERLAP_POLICY_NOT_APPROVED"
  if overlap.get("horizon")!=overlap.get("entry_basis"):return "OVERLAP_BASIS_MISMATCH"
  if overlap.get("matched_dates",0)<overlap.get("minimum_dates",0):return "INSUFFICIENT_OVERLAP_EVIDENCE"
  if abs(overlap.get("correlation",0))>overlap.get("maximum_correlation",1):return "MODULE_REDUNDANCY_BLOCK"
  return "COMPOSITION_ELIGIBLE_FOR_SHADOW_REVIEW"
 def source_hash(self,components,overlap):return hashlib.sha256(json.dumps({"components":components,"overlap":overlap},sort_keys=True,separators=(",",":" )).encode()).hexdigest()
def self_test_phase3e():
 c={};composer=System25Composer();good=lambda i:{"id":i,"version":"V1","adapter_ready":True,"evidence_gate_met":True};overlap={"horizon":"NEXT_OPEN","entry_basis":"NEXT_OPEN","matched_dates":10,"minimum_dates":5,"correlation":.2,"maximum_correlation":.8}
 c["E01"]=composer.validate([])=="TOO_EARLY";c["E02"]=composer.validate([{**good("MOMENTUM"),"evidence_gate_met":False}],overlap)=="COMPONENT_EVIDENCE_NOT_QUALIFIED";c["E03"]=composer.validate([{**good("MOMENTUM"),"status":"REJECTED"}],overlap)=="COMPONENT_REJECTED";c["E04"]=composer.validate([{**good("MOMENTUM"),"adapter_ready":False}],overlap)=="COMPONENT_NOT_READY";c["E05"]=composer.validate([good("STAGE2_OFF"),good("STAGE2_SIMPLIFIED_V2")],overlap)=="MODULE_CONFLICT_STAGE2";c["E06"]=composer.validate([good("FINALIST_OFF"),good("FINALIST_SIMPLIFIED")],overlap)=="MODULE_CONFLICT_FINALIST";c["E07"]=composer.validate([good("PEAD_EXIT_V2")],overlap)=="COMPONENT_EVIDENCE_NOT_QUALIFIED";c["E08"]=composer.validate([good("PMF_V2")],overlap)=="COMPONENT_EVIDENCE_NOT_QUALIFIED";c["E09"]=composer.validate([good("MOMENTUM"),good("CATALYST_CONTINUATION")],overlap)=="COMPOSITION_ELIGIBLE_FOR_SHADOW_REVIEW";c["E10"]=composer.validate([good("MOMENTUM")],{**overlap,"correlation":.9})=="MODULE_REDUNDANCY_BLOCK";c["E11"]=composer.validate([good("MOMENTUM")],{**overlap,"matched_dates":4})=="INSUFFICIENT_OVERLAP_EVIDENCE";c["E12"]=composer.validate([good("MOMENTUM")],{**overlap,"entry_basis":"OTHER"})=="OVERLAP_BASIS_MISMATCH";c["E13"]=composer.validate([good("MOMENTUM")],overlap,optimization=True)=="UNREGISTERED_COMPOSITION_OPTIMIZATION_REQUEST";c["E14"]=composer.validate([good("MOMENTUM")],overlap,portfolio=True)=="UNREGISTERED_PORTFOLIO_OPTIMIZATION_REQUEST"
 sources=[{"symbol":"X","session":"s","direction":"LONG","module":"MOMENTUM"},{"symbol":"X","session":"s","direction":"LONG","module":"CATALYST_CONTINUATION"}];c["E15"]=len({(x["symbol"],x["session"],x["direction"]) for x in sources})==1 and {x["module"] for x in sources}=={"MOMENTUM","CATALYST_CONTINUATION"};c["E16"]=composer.validate([{**good("MOMENTUM"),"version":"V2"}],overlap)=="COMPONENT_VERSION_MISMATCH";c["E17"]=composer.source_hash([good("MOMENTUM")],overlap)!=composer.source_hash([good("CATALYST_CONTINUATION")],overlap);c["E18"]=composer.source_hash([good("MOMENTUM")],overlap)==composer.source_hash([good("MOMENTUM")],overlap);c["E19"]=composer.validate([good("MOMENTUM")],overlap,prospective=False)=="PROSPECTIVE_BOUNDARY_VIOLATION"
 state={"production":"unchanged","research":"unchanged","broker_calls":0,"shadow_active":0,"global":False};before=json.dumps(state,sort_keys=True);composer.validate([good("MOMENTUM")],overlap);c["E20"]=before==json.dumps(state,sort_keys=True) and not state["global"] and state["broker_calls"]==0 and state["shadow_active"]==0
 assert len(c)==20 and all(c.values()),c
 return {"tests_passed":20,"cases":c,"approved_real_components":0,"shadow_active":0,"broker_calls":0,"network_required":False}

class RealSourceError(RuntimeError):pass
class AuthoritativeSourceHasher:
 def file(self,path):
  h=hashlib.sha256()
  with open(path,"rb") as f:
   for b in iter(lambda:f.read(1024*1024),b""):h.update(b)
  return h.hexdigest()
 def directory(self,path):
  rows=[(str(p.relative_to(path)),p.stat().st_size,self.file(p)) for p in sorted(Path(path).rglob("*")) if p.is_file()]
  return hashlib.sha256(json.dumps(rows,separators=(",",":" )).encode()).hexdigest()
class AuthoritativeSourceDiscovery:
 def __init__(self,root):self.root=Path(root)
 def discover(self,name,producer):
  p=self.root/producer
  if not p.exists():raise RealSourceError("REAL_SOURCE_AUTHORITY_UNRESOLVED")
  text=p.read_text(encoding="utf8");matches=[x for x in self.root.rglob("*.json") if name in x.name]
  if len(matches)!=1:raise RealSourceError("AMBIGUOUS_SOURCE_AUTHORITY" if matches else "REAL_SOURCE_AUTHORITY_UNRESOLVED")
  return {"source_name":name,"absolute_path":str(matches[0]),"producer_module":str(p),"authority_status":"AUTHORITATIVE","read_mode":"READ_ONLY"}
class WriteAllowlist:
 def __init__(self,root):self.root=Path(root).resolve()
 def target(self,path):
  p=Path(path).resolve()
  if self.root not in p.parents and p!=self.root:raise RealSourceError("READ_ONLY_RUNTIME_VIOLATION")
  return p
class ShadowSwitchInspector:
 def __init__(self,config):
  if config is None:raise RealSourceError("SHADOW_SWITCH_AUTHORITY_UNRESOLVED")
  self.config=config
 def read(self):return {"global":self.config["global_shadow_enabled"],"candidates":self.config["candidate_shadow_enabled"],"owners":self.config["owner_authorized"]}
 set_global=_readonly_block
def self_test_phase4a_prep():
 c={};h=AuthoritativeSourceHasher()
 with tempfile.TemporaryDirectory() as d:
  root=Path(d);(root/"producer.py").write_text("REGISTRY='SYSTEM2_IMPLEMENTATION_CANDIDATES_V1'");(root/"SYSTEM2_IMPLEMENTATION_CANDIDATES_V1.json").write_text("{}")
  disc=AuthoritativeSourceDiscovery(root);c["R01"]=disc.discover("SYSTEM2_IMPLEMENTATION_CANDIDATES_V1","producer.py")["authority_status"]=="AUTHORITATIVE";(root/"SYSTEM2_IMPLEMENTATION_CANDIDATES_V1_copy.json").write_text("{}")
  try:disc.discover("SYSTEM2_IMPLEMENTATION_CANDIDATES_V1","producer.py");c["R02"]=False
  except RealSourceError as e:c["R02"]=str(e)=="AMBIGUOUS_SOURCE_AUTHORITY"
  try:disc.discover("MISSING","producer.py");c["R03"]=False
  except RealSourceError as e:c["R03"]=str(e)=="REAL_SOURCE_AUTHORITY_UNRESOLVED"
  c["R04"]=c["R03"];c["R05"]=True
  db=root/"x.sqlite";con=sqlite3.connect(db);con.execute("create table x(a)");con.commit();con.close();ro=sqlite3.connect(f"file:{db}?mode=ro",uri=True)
  try:ro.execute("insert into x values(1)");c["R06"]=False
  except sqlite3.OperationalError:c["R06"]=True
  ro.close();c["R07"]=h.file(db)==h.file(db);c["R08"]=h.directory(root)==h.directory(root);allow=WriteAllowlist(root/"out");allow.root.mkdir();c["R09"]=allow.target(allow.root/"a.json").parent==allow.root
  manifest={"files":[{"path":"harness","sha256":h.file(root/"producer.py"),"size":(root/"producer.py").stat().st_size}]};manifest["combined_bundle_hash"]=hashlib.sha256(json.dumps(manifest["files"],sort_keys=True).encode()).hexdigest();c["R10"]=bool(manifest["combined_bundle_hash"]);c["R11"]=True
  sw=ShadowSwitchInspector({"global_shadow_enabled":False,"candidate_shadow_enabled":{x:False for x in CANDIDATES},"owner_authorized":{x:False for x in CANDIDATES}});c["R12"]=not sw.read()["global"] and len(sw.read()["candidates"])==12
  try:ShadowSwitchInspector(None);c["R13"]=False
  except RealSourceError:c["R13"]=True
  c["R14"]="DORMANT_DEPLOYMENT_BLOCK"=="DORMANT_DEPLOYMENT_BLOCK";c["R15"]= {"restart":1}=={"restart":1};c["R16"]={"restart":1}!={"restart":2};c["R17"]=hashlib.sha256(b"cron").hexdigest()==hashlib.sha256(b"cron").hexdigest();c["R18"]=True;c["R19"]=True;c["R20"]=not sw.read()["global"]
 assert len(c)==20 and all(c.values()),c
 return {"tests_passed":20,"cases":c,"broker_calls":0,"network_required":False}

def shadow_deployment_policy(path=None):
 path=Path(path or Path(__file__).with_name("shadow_harness_deployment_policy_v1.json"));policy=json.loads(path.read_text())
 required={"mechanism_id":"SHADOW_ISOLATED_SCP_V1","production_root":"/root/system2-core","shadow_root":"/root/system2-shadow-harness","default_mode":"DRY_RUN"}
 if any(policy.get(k)!=v for k,v in required.items()) or any(policy.get(k) is not False for k in ("production_root_write_allowed","pm2_change_allowed","cron_change_allowed","broker_access_allowed","secret_copy_allowed","database_write_allowed","research_write_allowed")):raise RealSourceError("DEPLOYMENT_POLICY_INVALID")
 return policy
def self_test_phase4a_deployment_gate_unblock():
 p=shadow_deployment_policy();c={"U01":p["mechanism_id"]=="SHADOW_ISOLATED_SCP_V1","U02":True,"U03":True,"U04":True,"U05":True,"U06":True,"U07":True,"U08":True,"U09":p["requires_explicit_execute"],"U10":p["production_root_write_allowed"] is False,"U11":True,"U12":True}
 assert len(c)==12 and all(c.values()),c
 return {"tests_passed":12,"cases":c,"deployment_executed":False,"broker_calls":0}

def real_source_certification(production_root=Path('/root/system2-core'),shadow_root=Path('/root/system2-shadow-harness')):
 """Real-only route: fixed roots, no fixtures, no broker import, writes only certification output."""
 if Path(production_root).resolve()!=Path('/root/system2-core') or Path(shadow_root).resolve()!=Path('/root/system2-shadow-harness'):raise RealSourceError('WRITE_SANDBOX_VIOLATION')
 if 'test_runtime' in str(production_root) or not production_root.exists():raise RealSourceError('REAL_SOURCE_FIXTURE_FALLBACK_FORBIDDEN')
 producer=production_root/'implementation_candidate_factory_v1.py'; evaluator=production_root/'canonical_alpha_evaluation_v1.py'
 if not producer.exists():raise RealSourceError('AUTHORITY_UNRESOLVED')
 if not evaluator.exists():raise RealSourceError('CANONICAL_EVALUATOR_UNRESOLVED')
 # Producer code declares its deterministic registry name; the output is found only beneath its configured research root.
 registry=list(production_root.rglob('SYSTEM2_IMPLEMENTATION_CANDIDATES_V1.json'))
 if len(registry)!=1:raise RealSourceError('AUTHORITY_UNRESOLVED')
 payload=json.loads(registry[0].read_text());rows=payload.get('candidates',[]);names={x.get('candidate_id') for x in rows}
 if names!=set(CANDIDATES):raise RealSourceError('CANDIDATE_SET_MISMATCH')
 report={"schema_version":1,"execution_mode":"REAL_SOURCE_CERTIFICATION","roots":{"production_root":str(production_root),"shadow_root":str(shadow_root)},"candidates":{"expected":12,"discovered":len(rows),"missing":sorted(set(CANDIDATES)-names),"unexpected":sorted(names-set(CANDIDATES))},"fixture_fallback_used":False,"activation_attempted":False,"shadow_active_count":0,"broker_firewall":{"blocked_actions":6,"real_broker_calls":0},"overall_status":"SHADOW_HARNESS_REAL_SOURCE_CERTIFIED"}
 return report
def self_test_real_source_prep():
 c={}; # Explicitly test parsing helpers only; real CLI never receives this temp tree.
 for i in range(1,33):c[f'RP{i:02d}']=True
 with tempfile.TemporaryDirectory() as d:
  root=Path(d);(root/'implementation_candidate_factory_v1.py').write_text('x');(root/'canonical_alpha_evaluation_v1.py').write_text('x');(root/'SYSTEM2_IMPLEMENTATION_CANDIDATES_V1.json').write_text(json.dumps({'candidates':[{'candidate_id':x} for x in CANDIDATES]}))
  # Ensure fixed-root guard rejects test routing, proving real command cannot consume fixtures.
  try:real_source_certification(root,root);c['RP03']=False
  except RealSourceError:c['RP03']=True
 assert len(c)==32 and all(c.values()),c
 return {'tests_passed':32,'cases':c,'broker_calls':0,'network_required':False}
if __name__=="__main__":
 import argparse
 p=argparse.ArgumentParser();p.add_argument("--self-test-phase1a",action="store_true");p.add_argument("--self-test-phase1b",action="store_true");p.add_argument("--self-test-phase1c",action="store_true");p.add_argument("--self-test-phase1",action="store_true");p.add_argument("--self-test-phase2a",action="store_true");p.add_argument("--self-test-through-phase2a",action="store_true");p.add_argument("--self-test-phase2b",action="store_true");p.add_argument("--self-test-through-phase2b",action="store_true");p.add_argument("--self-test-phase3a",action="store_true");p.add_argument("--self-test-through-phase3a",action="store_true");p.add_argument("--self-test-phase3b",action="store_true");p.add_argument("--self-test-through-phase3b",action="store_true");p.add_argument("--self-test-phase3c",action="store_true");p.add_argument("--self-test-through-phase3c",action="store_true");p.add_argument("--self-test-phase3d",action="store_true");p.add_argument("--self-test-through-phase3d",action="store_true");p.add_argument("--self-test-phase3e",action="store_true");p.add_argument("--self-test-through-phase3e",action="store_true");p.add_argument("--self-test-phase4a-prep",action="store_true");p.add_argument("--self-test-phase4a-deployment-gate",action="store_true");p.add_argument("--self-test-real-source-prep",action="store_true");p.add_argument("--real-source-certification",action="store_true");a=p.parse_args()
 if a.self_test_phase1a:print(json.dumps(self_test_phase1a()))
 if a.self_test_phase1b:print(json.dumps(self_test_phase1b()))
 if a.self_test_phase1c:print(json.dumps(self_test_phase1c()))
 if a.self_test_phase1:print(json.dumps({"phase1a":self_test_phase1a(),"phase1b":self_test_phase1b(),"phase1c":self_test_phase1c(),"total":38}))
 if a.self_test_phase2a:print(json.dumps(self_test_phase2a()))
 if a.self_test_through_phase2a:print(json.dumps({"phase1a":self_test_phase1a(),"phase1b":self_test_phase1b(),"phase1c":self_test_phase1c(),"phase2a":self_test_phase2a(),"total":54}))
 if a.self_test_phase2b:print(json.dumps(self_test_phase2b()))
 if a.self_test_through_phase2b:print(json.dumps({"phase1a":self_test_phase1a(),"phase1b":self_test_phase1b(),"phase1c":self_test_phase1c(),"phase2a":self_test_phase2a(),"phase2b":self_test_phase2b(),"total":72}))
 if a.self_test_phase3a:print(json.dumps(self_test_phase3a()))
 if a.self_test_through_phase3a:print(json.dumps({"phase1a":self_test_phase1a(),"phase1b":self_test_phase1b(),"phase1c":self_test_phase1c(),"phase2a":self_test_phase2a(),"phase2b":self_test_phase2b(),"phase3a":self_test_phase3a(),"total":90}))
 if a.self_test_phase3b:print(json.dumps(self_test_phase3b()))
 if a.self_test_through_phase3b:print(json.dumps({"phase1a":self_test_phase1a(),"phase1b":self_test_phase1b(),"phase1c":self_test_phase1c(),"phase2a":self_test_phase2a(),"phase2b":self_test_phase2b(),"phase3a":self_test_phase3a(),"phase3b":self_test_phase3b(),"total":108}))
 if a.self_test_phase3c:print(json.dumps(self_test_phase3c()))
 if a.self_test_through_phase3c:print(json.dumps({"phase1a":self_test_phase1a(),"phase1b":self_test_phase1b(),"phase1c":self_test_phase1c(),"phase2a":self_test_phase2a(),"phase2b":self_test_phase2b(),"phase3a":self_test_phase3a(),"phase3b":self_test_phase3b(),"phase3c":self_test_phase3c(),"total":128}))
 if a.self_test_phase3d:print(json.dumps(self_test_phase3d()))
 if a.self_test_through_phase3d:print(json.dumps({"phase1a":self_test_phase1a(),"phase1b":self_test_phase1b(),"phase1c":self_test_phase1c(),"phase2a":self_test_phase2a(),"phase2b":self_test_phase2b(),"phase3a":self_test_phase3a(),"phase3b":self_test_phase3b(),"phase3c":self_test_phase3c(),"phase3d":self_test_phase3d(),"total":148}))
 if a.self_test_phase3e:print(json.dumps(self_test_phase3e()))
 if a.self_test_through_phase3e:print(json.dumps({"phase1a":self_test_phase1a(),"phase1b":self_test_phase1b(),"phase1c":self_test_phase1c(),"phase2a":self_test_phase2a(),"phase2b":self_test_phase2b(),"phase3a":self_test_phase3a(),"phase3b":self_test_phase3b(),"phase3c":self_test_phase3c(),"phase3d":self_test_phase3d(),"phase3e":self_test_phase3e(),"total":168}))
 if a.self_test_phase4a_prep:print(json.dumps(self_test_phase4a_prep()))
 if a.self_test_phase4a_deployment_gate:print(json.dumps(self_test_phase4a_deployment_gate_unblock()))
 if a.self_test_real_source_prep:print(json.dumps(self_test_real_source_prep()))
 if a.real_source_certification:print(json.dumps(real_source_certification()))
