#!/usr/bin/env node
/*
 * Read-only dashboard population diagnostic.  It deliberately reads fund.json
 * once, never starts HTTP authentication, imports no execution module, and
 * writes only its requested diagnostic JSON artifact.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const FUND = process.env.SYSTEM2_FUND_JSON || '/root/fund-system/data/fund.json';
const OUT_DIR = process.env.SYSTEM2_DIAGNOSTIC_DIR || '/root/system2-core/reports';
const INVALID = new Set(['TEST', 'TESTPUB', 'SOCIALTEST', 'FWONK', 'ALL']);
const SUSPECT = new Set(['STM', 'AXON', 'CRDO', 'COO', 'FDXF', 'FOX', 'FOXA', 'SEM', 'CNTA']);
const n = value => value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? null : Number(value);
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const id = row => String(row.id ?? 'MISSING_ID');
const isInvalid = row => !row?.ticker || INVALID.has(String(row.ticker).toUpperCase());
const isPmfLate = row => String(row?.pmf_stamp_cohort || row?.pmf_entry_cohort || row?.pmf_cohort || '').toUpperCase() === 'PMF_LATE';
const isPrimaryPmf = row => row?.pmf_confirmed_at_stamp === true && !isPmfLate(row);

function cloneAndDerive(source) {
  const row = { ...source };
  if (row.original_entry == null && row.entry != null) row.original_entry = n(row.entry);
  if (row.original_stop == null && row.stop != null) row.original_stop = n(row.stop);
  if (row.original_target == null && row.target != null) row.original_target = n(row.target);
  const entry = n(row.original_entry ?? row.actual_entry_price ?? row.paper_entry_price ?? row.entry);
  const stop = n(row.original_stop ?? row.stop);
  const risk = n(row.original_risk_per_share) ?? (entry != null && stop != null ? Number(Math.abs(entry - stop).toFixed(4)) : n(row.risk_per_share));
  if (row.r_calculation_suspect == null) {
    const suspectR = n(row.paper_exit_r ?? row.actual_r ?? row.unrealized_r);
    if (isInvalid(row) || (suspectR != null && (Math.abs(suspectR) > 6 || suspectR < -3))) row.r_calculation_suspect = true;
    if (entry != null && n(row.actual_entry_price) != null && risk > 0 && Math.abs(n(row.actual_entry_price) - entry) > risk * 3) row.r_calculation_suspect = true;
  }
  let paper_status = 'OPEN';
  if (isInvalid(row) || row.r_calculation_suspect) paper_status = 'INVALID';
  else if (n(row.actual_entry_price) != null && n(row.actual_exit_price) == null) paper_status = 'OPEN';
  else if (n(row.actual_exit_price) != null || ['TARGET', 'STOP'].includes(row.paper_exit_reason) || ['TARGET', 'STOP'].includes(row.hit) || Number(row.scored_stage) >= 10 || row.hit === 'TIME') paper_status = 'CLOSED';
  return { ...row, paper_status, _entry: entry, _stop: stop, _target: n(row.original_target ?? row.target) };
}
function resolvedR(row) {
  if (isInvalid(row)) return null;
  const exit = n(row.paper_exit_price);
  const risk = row._entry != null && row._stop != null ? Math.abs(row._entry - row._stop) : null;
  const statusResolved = row.paper_status !== 'OPEN';
  let quarantined = false;
  // Match canonicalQuarantineReasons: a non-resolved row has no quarantine
  // reasons, so its persisted canonical_r remains a valid legacy fallback.
  if (statusResolved) {
    const calculated = exit != null && row._entry != null && row._stop != null && row._entry !== row._stop ? (exit - row._entry) / (row._entry - row._stop) : null;
    quarantined = row.cohort_stats_excluded === true || row.r_calculation_suspect === true ||
      (risk != null && risk < Math.abs(row._entry) * .005) || (calculated != null && Math.abs(calculated) > 10) ||
      (exit != null && row._target != null && risk != null && (exit < Math.min(row._stop, row._target) - 5 * risk || exit > Math.max(row._stop, row._target) + 5 * risk));
  }
  if (quarantined) return null;
  const stored = n(row.canonical_r);
  if (stored != null && row.canonical_r_quarantined !== true) return stored;
  if (!statusResolved || exit == null || !risk || row._entry === row._stop) return null;
  return Number(((exit - row._entry) / (row._entry - row._stop)).toFixed(4));
}
function displayable(row) { return row.paper !== false && !isInvalid(row) && !SUSPECT.has(String(row.ticker || '').toUpperCase()) && row.r_calculation_suspect !== true; }
function rSource(row) {
  if (n(row.actual_entry_price) != null && n(row.actual_exit_price) != null) return 'REAL_FILL_R';
  if (n(row.trigger_price ?? row.price_at_alert ?? row.entry_trigger_price ?? row.live_price_at_entry) != null) return 'TRIGGER_PRICE_R';
  if (n(row.entry_day_price ?? row.price_at_signal ?? row.pre_market_price ?? row.price ?? row.previous_close ?? row.current_price) != null) return 'ESTIMATED_FILL_PROXY_R';
  return resolvedR(row) != null ? 'MODELED_ONLY' : 'UNKNOWN';
}
function membership(rows) { const ids = rows.map(id).sort(); return { count: ids.length, ids, sha256: sha(ids.join('\n')) }; }
function median(values) { const a = values.slice().sort((x,y)=>x-y); return a.length ? a[Math.floor(a.length / 2)] : null; }
function stat(rows, key) { const v = rows.map(key).filter(x => x != null); const w = v.filter(x=>x>0), l=v.filter(x=>x<0); return { n:v.length, wins:w.length, losses:l.length, win_rate:v.length ? w.length/v.length : null, mean:v.length ? v.reduce((a,b)=>a+b,0)/v.length : null, median:median(v), profit_factor:l.length ? w.reduce((a,b)=>a+b,0)/Math.abs(l.reduce((a,b)=>a+b,0)) : null }; }
function gitHead() { try { return execFileSync('git', ['-C', '/root/system2-core', 'rev-parse', 'HEAD'], { encoding:'utf8', stdio:['ignore','pipe','ignore'] }).trim(); } catch { return 'UNKNOWN_NOT_A_GIT_CHECKOUT'; } }
function plainRow(row, extra={}) { return { idea_id:id(row), trade_id:row.trade_id ?? null, ticker:row.ticker ?? null, date:row.date ?? null, paper_status:row.paper_status, canonicalResolvedR:resolvedR(row), legacy: String(row.date || '') < '2026-06-09', v2: String(row.date || '') >= '2026-06-09', actual_entry_price:n(row.actual_entry_price), actual_exit_price:n(row.actual_exit_price), fill_source:row.fill_source ?? null, real_r_fill_source:row.real_r_fill_source ?? null, r_source:rSource(row), ...extra }; }

if (process.argv[2] === '--verify' && process.argv[3]) {
  // Validation replay intentionally reads the immutable diagnostic artifact, not
  // live state.  This is how a second check proves the exact frozen snapshot.
  const prior = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const fingerprints = Object.fromEntries(Object.entries(prior.sets || {}).map(([k,v]) => [k, { count:v.count, sha256:v.sha256 }]));
  console.log(JSON.stringify({ ok:true, verified_snapshot_token:prior.snapshot_token, sets:fingerprints }));
  process.exit(0);
}

const raw = fs.readFileSync(FUND); // Exactly one fund.json read.
const fund = JSON.parse(raw);
const { sessions, ...tradingState } = fund; // auth-only sessions are explicitly excluded from the stable token.
const rows = (fund.ideas || []).map(cloneAndDerive);
const perf = rows.filter(r=>r.paper_status !== 'INVALID');
const score = rows.filter(displayable);
const today = new Date().toISOString().slice(0,10);
const from = new Date(Date.now() - 30*86400000).toISOString().slice(0,10);
const statistics = perf.filter(r => String(r.date || '') >= from && String(r.date || '') <= today);
const performanceResolved = perf.filter(r=>resolvedR(r) != null);
const scoreboardResolved = score.filter(r=>resolvedR(r) != null);
const active = score.filter(r=>r.paper_status === 'OPEN');
const v2Clean = scoreboardResolved.filter(r=>String(r.date || '') >= '2026-06-09');
const pmfPerformance = v2Clean.filter(isPrimaryPmf);
const pmfScoreboard = score.filter(isPrimaryPmf);
const alpacaPmf = rows.filter(r=>(r.fill_source === 'alpaca_paper' || r.real_r_fill_source === 'alpaca_paper' || r.alpaca_order_id) && r.pmf_confirmed_at_fill === true && r.test_order !== true);
const actionThreshold = 3;
const actions = active.map(r => {
  const current=n(r.current_price), stop=r._stop, target=r._target;
  const ds=current != null && stop != null ? ((current-stop)/current)*100 : null;
  const dt=current != null && target != null ? ((target-current)/current)*100 : null;
  const classification = ds == null || dt == null ? 'INVALID' : ds < 0 ? 'STOP_CROSSED' : dt < 0 ? 'TARGET_CROSSED' : ds <= actionThreshold ? 'NEAR_STOP' : dt <= actionThreshold ? 'NEAR_TARGET' : 'NORMAL';
  return { ...plainRow(r), current, entry:r._entry, stop, target, distance_to_stop_pct:ds, distance_to_target_pct:dt, current_displayed_action:(ds != null && ds <= actionThreshold) ? 'near_stop' : (dt != null && dt <= actionThreshold) ? 'near_target' : null, independent_classification:classification };
});
const extreme = score.map(r => {
  const exit=n(r.paper_exit_price), estimated=n(r.entry_day_price ?? r.price_at_signal ?? r.pre_market_price ?? r.price ?? r.previous_close ?? r.current_price);
  const trueR = estimated != null && exit != null && r._stop != null && estimated !== r._stop ? (exit-estimated)/Math.abs(estimated-r._stop) : null;
  const source=rSource(r); const classification=source==='REAL_FILL_R' ? 'VALID_REAL_FILL' : (estimated == null ? 'INSUFFICIENT_DATA' : Math.abs(trueR ?? 0)>3 ? 'STALE_PRICE_PROXY' : 'VALID_PROXY_LOW_CONFIDENCE');
  return { ...plainRow(r), modeled_entry:r._entry, estimated_fill:estimated, actual_fill:n(r.actual_entry_price), stop:r._stop, target:r._target, exit, risk_per_share:estimated != null && r._stop != null ? Math.abs(estimated-r._stop) : null, pnl_per_share:estimated != null && exit != null ? exit-estimated : null, stored_r:resolvedR(r), recomputed_proxy_r:trueR, true_r_fill_source:source, classification };
}).filter(r=>r.recomputed_proxy_r != null).sort((a,b)=>Math.abs(b.recomputed_proxy_r)-Math.abs(a.recomputed_proxy_r)).slice(0,30);
const snapshotCore = JSON.stringify({ tradingState, score_code_sha256:fs.existsSync('/root/fund-system/server/scoring-endpoints.cjs') ? sha(fs.readFileSync('/root/fund-system/server/scoring-endpoints.cjs')) : null });
const token = sha(snapshotCore);
const out = {
  schema:'dashboard_truth_snapshot_v1', snapshot_token:token,
  metadata:{ created_at_utc:new Date().toISOString(), git_head:gitHead(), fund_path:FUND, fund_sha256:sha(raw), fund_mtime_utc:fs.statSync(FUND).mtime.toISOString(), excluded_from_token:['sessions'], source_counts:{ideas:rows.length,sessions:Array.isArray(sessions)?sessions.length:0} },
  predicates:{ performance:'derived paper_status != INVALID', scoreboard:'displayableIdea', statistics:`performance rows dated ${from} through ${today}`, v2_clean:'displayable + canonical resolved + date >= 2026-06-09', action_near:'0 <= distance <= 3 (independent bounded diagnostic)' },
  populations:{
    performance:perf.map(r=>plainRow(r,{performance_included:true,performance_resolved:resolvedR(r)!=null,performance_open:r.paper_status==='OPEN',performance_invalid:r.paper_status==='INVALID'})),
    scoreboard:score.map(r=>plainRow(r,{scoreboard_included:true,scoreboard_resolved:resolvedR(r)!=null,scoreboard_open:r.paper_status==='OPEN'})),
    statistics:statistics.map(r=>plainRow(r,{statistics_included:true,statistics_entered:n(r.actual_entry_price)!=null,statistics_resolved:resolvedR(r)!=null,statistics_real_fill:rSource(r)==='REAL_FILL_R',statistics_proxy:rSource(r)==='ESTIMATED_FILL_PROXY_R'})),
    decision:{ active_trades:active.map(r=>plainRow(r,{classification:n(r.actual_entry_price)!=null ? (r.alpaca_order_id ? 'BROKER_ENTERED' : 'PAPER_ENTERED') : (r.paper_exit_reason || r.hit ? 'RESOLVED_STALE' : 'WATCHING')})), active_pre_market_favourable:active.filter(isPrimaryPmf).map(r=>plainRow(r,{classification:n(r.actual_entry_price)!=null ? 'BROKER_ENTERED' : 'WATCHLIST'})) },
    v2_clean:v2Clean.map(r=>plainRow(r)), pmf:{ performance:pmfPerformance.map(plainRow), scoreboard:pmfScoreboard.map(plainRow), alpaca:alpacaPmf.map(plainRow), active:active.filter(isPrimaryPmf).map(plainRow) }, actions, extreme_r:extreme,
    funnel:(fund.system2_stage_details || []).slice(-1)[0] || null,
    health_alerts:{ pmf_v1_retirement:{ source:'scanner_config/env not read by this utility', current_live_condition:'UNKNOWN_LOCAL_CLI' }, pmf_v2_collector:{ source:'no persisted alert row discovered', current_live_condition:'UNKNOWN' }, cron:{ source:'not read by utility', current_live_condition:'UNKNOWN' }, auto_exec_sync:{ source:'not read by utility', current_live_condition:'UNKNOWN' } }
  },
  sets:{ performance:membership(perf), performance_resolved:membership(performanceResolved), performance_open:membership(perf.filter(r=>r.paper_status==='OPEN')), scoreboard:membership(score), scoreboard_resolved:membership(scoreboardResolved), scoreboard_open:membership(active), statistics:membership(statistics), statistics_resolved:membership(statistics.filter(r=>resolvedR(r)!=null)), v2_clean:membership(v2Clean), pmf_performance:membership(pmfPerformance), pmf_scoreboard:membership(pmfScoreboard), pmf_alpaca:membership(alpacaPmf), pmf_active:membership(active.filter(isPrimaryPmf)) },
  intersections:{ pmf_performance_minus_scoreboard:pmfPerformance.filter(r=>!pmfScoreboard.some(x=>id(x)===id(r))).map(id), pmf_scoreboard_minus_performance:pmfScoreboard.filter(r=>!pmfPerformance.some(x=>id(x)===id(r))).map(id), resolved_open_intersection:scoreboardResolved.filter(r=>active.some(x=>id(x)===id(r))).map(id) },
  metrics:{ v2_clean:stat(v2Clean,resolvedR), pmf_performance:stat(pmfPerformance,resolvedR), pmf_alpaca:stat(alpacaPmf,r=>n(r.real_r)), r_sources:Object.fromEntries(['REAL_FILL_R','TRIGGER_PRICE_R','ESTIMATED_FILL_PROXY_R','MODELED_ONLY','UNKNOWN'].map(k=>[k,v2Clean.filter(r=>rSource(r)===k).length])), action_counts:actions.reduce((a,r)=>(a[r.independent_classification]=(a[r.independent_classification]||0)+1,a),{}) }
};
if (process.argv.includes('--stdout')) process.stdout.write(JSON.stringify(out));
else {
  fs.mkdirSync(OUT_DIR, { recursive:true });
  const file = path.join(OUT_DIR, `dashboard_truth_snapshot_${out.metadata.created_at_utc.replace(/[:.]/g,'-')}.json`);
  fs.writeFileSync(file, JSON.stringify(out, null, 2));
  const sets = Object.fromEntries(Object.entries(out.sets).map(([k,v]) => [k, { count:v.count, sha256:v.sha256 }]));
  console.log(JSON.stringify({ ok:true, file, snapshot_token:token, sets }));
}
