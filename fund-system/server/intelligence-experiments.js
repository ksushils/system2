import pg from 'pg';
import { analyticsFirewallSql } from './analytics-firewall.js';
const pool = process.env.DATABASE_URL ? new pg.Pool({ connectionString:process.env.DATABASE_URL, max:3, application_name:'fund-experiments' }) : null;
let ready=false, lastDaily='';

async function stats(scanner, hash, since='30 days') {
  const {rows:sourceRows}=await pool.query(`SELECT source_type,count(*)::int n,avg(ret_1d)::float8 avg_ret_1d,
    100*avg((ret_1d>0)::int)::float8 pct_won FROM signal_outcomes o WHERE scanner=$1 AND ret_1d IS NOT NULL
    AND event_ts>=now()-$3::interval AND ($2::text IS NULL OR config_hash=$2) AND ${analyticsFirewallSql('o')} GROUP BY source_type`,[scanner,hash,since]);
  const bySource=Object.fromEntries(sourceRows.map(r=>[r.source_type,{n:r.n,avg_ret_1d:r.avg_ret_1d,pct_won:r.pct_won}]));const signal=bySource.signal||{n:0,avg_ret_1d:null,pct_won:null},total=sourceRows.reduce((s,r)=>s+Number(r.n),0);
  const {rows:reasons}=await pool.query(`SELECT coalesce(data->>'reason','UNKNOWN') reason,count(*)::int n FROM rejections r
    WHERE scanner=$1 AND created_at>=now()-$2::interval AND ${analyticsFirewallSql('','r')} GROUP BY 1 ORDER BY n DESC`,[scanner,since]);
  const {rows:[e]}=await pool.query(`SELECT count(*)::int n,avg(CASE WHEN coalesce(risk_amount,0)>0 THEN coalesce(pnl_net,pnl)/risk_amount END)::float8 expectancy_r
    FROM trades WHERE scanner=$1 AND status='CLOSED' AND coalesce(pnl_net,pnl) IS NOT NULL AND closed_at>=now()-$2::interval`,[scanner,since]);
  return {n:signal.n,avg_ret_1d:signal.avg_ret_1d,pct_won:signal.pct_won,total_n:total,by_source:bySource,population_mix:Object.fromEntries(sourceRows.map(r=>[r.source_type,total?+(100*Number(r.n)/total).toFixed(2):0])),rejection_rate_by_reason:Object.fromEntries(reasons.map(r=>[r.reason,r.n])),expectancy_r:e.n>=10?e.expectancy_r:null,closed_trades:e.n};
}
export const experimentStats=stats;

export async function initExperiments() {
  if(!pool||ready)return !!pool;
  await pool.query(`CREATE TABLE IF NOT EXISTS change_evaluations(
    id bigserial primary key,changelog_id bigint unique,scanner text,parameter text,old_value text,new_value text,
    changed_at timestamptz,config_hash_before text,config_hash_after text,before_stats jsonb,after_stats jsonb,
    n_before int default 0,n_after int default 0,verdict text check(verdict in('IMPROVED','WORSE','NO_EFFECT','INSUFFICIENT')),
    evaluated_at timestamptz,status text not null default 'OPEN' check(status in('OPEN','CLOSED')));
    CREATE TABLE IF NOT EXISTS challenger_evals(
    id bigserial primary key,scanner text not null,computed_at timestamptz default now(),champion_hash text,
    challenger_params jsonb,n_disagreements int,challenger_only_n int,champion_only_n int,
    challenger_only_avg_ret_1d numeric,champion_only_avg_ret_1d numeric,top3_holds boolean,halves_hold boolean,
    verdict text,details jsonb default '{}'::jsonb);
    ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS config_hash text;
    ALTER TABLE change_evaluations ADD COLUMN IF NOT EXISTS simulated boolean NOT NULL DEFAULT false;
  `); ready=true; return true;
}

export async function createChangeEvaluation(changelog, beforeHash, afterHash) {
  await initExperiments(); if(!pool||!changelog?.scanner||changelog.approved_by==='challenger')return null;
  const before=await stats(changelog.scanner,beforeHash);
  const {rows}=await pool.query(`INSERT INTO change_evaluations(changelog_id,scanner,parameter,old_value,new_value,changed_at,config_hash_before,config_hash_after,before_stats,n_before,status)
    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'OPEN') ON CONFLICT(changelog_id) DO NOTHING RETURNING *`,
    [changelog.id,changelog.scanner,changelog.parameter,changelog.old_value,changelog.new_value,changelog.date,beforeHash,afterHash,before,before.n]);
  return rows[0]||null;
}

function verdict(before,after,ageDays){
  if(Number(before?.n||0)<30||before?.avg_ret_1d===null||before?.avg_ret_1d===undefined)return 'INSUFFICIENT';
  if(after.n<30)return ageDays>=30?'INSUFFICIENT':null;
  if(after.avg_ret_1d===null||after.avg_ret_1d===undefined)return 'INSUFFICIENT';
  const delta=Number(after.avg_ret_1d)-Number(before.avg_ret_1d);
  if(delta>=.0015)return 'IMPROVED'; if(delta<=-.0015)return 'WORSE'; return 'NO_EFFECT';
}
export async function evaluateChanges({force=false,simulatedAfter=null,id=null,evaluation_id=null}={}){
  await initExperiments();
  if(simulatedAfter){const target=evaluation_id??id;if(!target)throw new Error('evaluation_id is required for simulation');const {rows:[row]}=await pool.query(`SELECT * FROM change_evaluations WHERE id=$1`,[target]);if(!row)throw new Error('evaluation not found');const age=(Date.now()-new Date(row.changed_at))/86400000;return [{...row,after_stats:simulatedAfter,n_after:simulatedAfter.n,verdict:verdict(row.before_stats,simulatedAfter,age)||'INSUFFICIENT',simulated:true,persisted:false}];}
  const {rows}=await pool.query(`SELECT * FROM change_evaluations WHERE status='OPEN' AND ($1::bigint IS NULL OR id=$1)`,[id]); const closed=[];
  for(const row of rows){const age=(Date.now()-new Date(row.changed_at))/86400000; const after=await stats(row.scanner,row.config_hash_after);
    const v=verdict(row.before_stats,after,age); if(!v)continue;
    const final=v||(after.n<30?'INSUFFICIENT':'NO_EFFECT');
    const {rows:[saved]}=await pool.query(`UPDATE change_evaluations SET after_stats=$2,n_after=$3,verdict=$4,evaluated_at=now(),status='CLOSED' WHERE id=$1 RETURNING *`,[row.id,after,after.n,final]); closed.push(saved);}
  return closed;
}

function thresholdDecision(param,value,data){
  const n=Number(value); if(!Number.isFinite(n))return null;
  if(param==='MIN_SIGNAL_SCORE'){
    const explicit=Number(data.score??data.quality_score??data.signal_score);
    const match=String(data.reason||data.message||'').match(/\bScore\s+(-?\d+(?:\.\d+)?)/i);
    const score=Number.isFinite(explicit)?explicit:Number(match?.[1]);
    return Number.isFinite(score)?score>=n:null;
  }
  if(param==='MIN_VOLUME_RATIO'){const v=Number(data.volume_ratio);return Number.isFinite(v)?v>=n:null;}
  if(param==='RSI_MIN'){const v=Number(data.rsi);return Number.isFinite(v)?v>=n:null;}
  if(param==='RSI_MAX'){const v=Number(data.rsi);return Number.isFinite(v)?v<=n:null;}
  return null;
}
export async function evaluateChallenger(scanner){
  await initExperiments(); const {rows:params}=await pool.query(`SELECT c.param,c.value challenger,p.value champion FROM scanner_params c JOIN scanner_params p USING(scanner,param) WHERE c.scanner=$1 AND c.variant='challenger' AND p.variant='champion'`,[scanner]);
  if(!params.length)return null;
  const {rows}=await pool.query(`SELECT o.source_type,o.source_id,o.event_ts,o.ret_1d::float8,coalesce(s.data,r.data,'{}') data FROM signal_outcomes o LEFT JOIN signals s ON o.source_type='signal' AND s.id::text=o.source_id LEFT JOIN rejections r ON o.source_type='rejection' AND r.id::text=o.source_id WHERE o.scanner=$1 AND o.ret_1d IS NOT NULL AND o.event_ts>=now()-interval '30 days' AND ${analyticsFirewallSql('o','r')} ORDER BY o.event_ts`,[scanner]);
  let representableRows=0; const disagreements=[];
  for(const row of rows){let champ=true,chall=true,usable=true;for(const p of params){const a=thresholdDecision(p.param,p.champion,row.data),b=thresholdDecision(p.param,p.challenger,row.data);if(a===null||b===null){usable=false;break;}champ=champ&&a;chall=chall&&b;}if(!usable)continue;representableRows++;if(champ!==chall)disagreements.push({...row,champ,chall});}
  let verdict='NOT_EVALUABLE',top3=false,halves=false,halfCounts=[0,0]; const co=disagreements.filter(x=>x.chall), po=disagreements.filter(x=>x.champ);
  const avg=a=>a.length?a.reduce((s,x)=>s+x.ret_1d,0)/a.length:null, delta=()=>Number(avg(co)||0)-Number(avg(po)||0);
  if(representableRows>0){if(disagreements.length<30)verdict='INSUFFICIENT';else{const sorted=[...disagreements].sort((a,b)=>Math.abs(b.ret_1d)-Math.abs(a.ret_1d)).slice(3);top3=Math.sign(delta())===Math.sign(Number(avg(sorted.filter(x=>x.chall))||0)-Number(avg(sorted.filter(x=>x.champ))||0));const windowStart=Date.now()-30*86400000,mid=windowStart+15*86400000,ds=[disagreements.filter(x=>new Date(x.event_ts).getTime()<mid),disagreements.filter(x=>new Date(x.event_ts).getTime()>=mid)];halfCounts=ds.map(h=>h.length);halves=ds.every(h=>h.length>=15&&Math.sign(delta())===Math.sign(Number(avg(h.filter(x=>x.chall))||0)-Number(avg(h.filter(x=>x.champ))||0)));verdict=top3&&halves?(delta()>0?'CHALLENGER_BETTER':delta()<0?'CHAMPION_BETTER':'NO_DIFFERENCE'):'NO_DIFFERENCE';}}
  const {rows:[saved]}=await pool.query(`INSERT INTO challenger_evals(scanner,challenger_params,n_disagreements,challenger_only_n,champion_only_n,challenger_only_avg_ret_1d,champion_only_avg_ret_1d,top3_holds,halves_hold,verdict,details) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *`,[scanner,Object.fromEntries(params.map(p=>[p.param,p.challenger])),disagreements.length,co.length,po.length,avg(co),avg(po),top3,halves,verdict,{params,representable_rows:representableRows,half_counts:halfCounts,split:'window_start_plus_15_days'}]);return saved;
}

export function attachExperiments(app,{adminOnly,getConfigHash,globalConfig,validateParamValue}){
  app.get('/api/intelligence/changes',adminOnly,async(req,res)=>{await initExperiments();const p=[],w=[];if(req.query.scanner){p.push(String(req.query.scanner).toLowerCase());w.push(`scanner=$${p.length}`);}const {rows}=await pool.query(`SELECT * FROM change_evaluations ${w.length?'WHERE '+w.join(' AND '):''} ORDER BY changed_at DESC`,p);res.json({rows});});
  app.post('/api/intelligence/changes/run',adminOnly,async(req,res)=>res.json({rows:await evaluateChanges(req.body||{})}));
  app.post('/api/params/challenger',adminOnly,async(req,res)=>{await initExperiments();const scanner=String(req.body?.scanner||'').toLowerCase(),param=String(req.body?.param||''),value=String(req.body?.value??''),reason=String(req.body?.reason||'');if(!scanner||!param||!value||!reason)return res.status(400).json({error:'scanner,param,value,reason required'});const before=await getConfigHash(scanner,globalConfig());const {rows:[champ]}=await pool.query(`SELECT * FROM scanner_params WHERE scanner=$1 AND param=$2 AND variant='champion'`,[scanner,param]);if(!champ)return res.status(400).json({error:'unknown champion parameter'});const valid=validateParamValue(champ,value,{confirm:req.body?.confirm===true});if(!valid.ok){await pool.query(`INSERT INTO intelligence_changelog(scanner,parameter,old_value,new_value,reason,approved_by) VALUES($1,$2,$3,$4,$5,'challenger-rejected')`,[scanner,`REJECTED:${param}`,champ.value,String(value),valid.error]).catch(()=>{});return res.status(400).json({error:valid.error,requires_confirm:valid.requires_confirm===true,bound:valid.bound||null});}const {rows:prior}=await pool.query(`SELECT * FROM scanner_params WHERE scanner=$1 AND variant='challenger'`,[scanner]);for(const old of prior)await pool.query(`INSERT INTO intelligence_changelog(scanner,parameter,old_value,new_value,reason,approved_by) VALUES($1,$2,$3,NULL,$4,'challenger')`,[scanner,`CHALLENGER_REMOVED:${old.param}`,old.value,`Replaced by ${param}: ${reason}`]);await pool.query(`DELETE FROM scanner_params WHERE scanner=$1 AND variant='challenger'`,[scanner]);await pool.query(`INSERT INTO scanner_params(scanner,param,value,value_type,variant,updated_by) VALUES($1,$2,$3,$4,'challenger','admin-api')`,[scanner,param,valid.value,champ.value_type]);const {rows:[log]}=await pool.query(`INSERT INTO intelligence_changelog(scanner,parameter,old_value,new_value,reason,approved_by) VALUES($1,$2,$3,$4,$5,'challenger') RETURNING *`,[scanner,param,champ.value,valid.value,reason]);res.json({status:'ok',champion_unchanged:champ.value,challenger:valid.value,replaced_challengers:prior.length,config_hash_champion:before,changelog:log});});
  app.get('/api/intelligence/challengers',adminOnly,async(req,res)=>{await initExperiments();const {rows}=await pool.query(`SELECT * FROM challenger_evals WHERE ($1::text IS NULL OR scanner=$1) ORDER BY computed_at DESC`,[req.query.scanner||null]);res.json({rows});});
  app.post('/api/intelligence/challengers/run',adminOnly,async(req,res)=>res.json(await evaluateChallenger(String(req.body?.scanner||'').toLowerCase())));
}

export function startExperimentJobs(){setInterval(()=>{const d=new Date().toISOString().slice(0,10);if(new Date().getUTCHours()!==5||lastDaily===d)return;lastDaily=d;evaluateChanges().catch(e=>console.error('[change-eval]',e.message));pool?.query(`SELECT DISTINCT scanner FROM scanner_params WHERE variant='challenger'`).then(r=>Promise.all(r.rows.map(x=>evaluateChallenger(x.scanner)))).catch(e=>console.error('[challenger-eval]',e.message));},300000).unref?.();}
