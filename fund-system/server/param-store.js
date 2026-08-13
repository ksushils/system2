import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { getTopFilterVerdictsByScanner } from './layer2.js';
import crypto from 'crypto';
import { createChangeEvaluation } from './intelligence-experiments.js';
import { latencyStats } from './latency-monitor.js';
import { analyticsFirewallSql } from './analytics-firewall.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const SCANNER_ORDER = [
  'fmp',
  'fmp_alpaca',
  'forex',
  'comm',
  'pa',
  'failed_breakout',
  'volume',
  'mean_reversion',
  'indices',
  'crypto'
];

const ALIASES = {
  fb: 'failed_breakout',
  failedbo: 'failed_breakout',
  failed_breakout: 'failed_breakout',
  vp: 'volume',
  vol: 'volume',
  volume_profile: 'volume',
  volume: 'volume',
  mr: 'mean_reversion',
  meanreversion: 'mean_reversion',
  mean_reversion: 'mean_reversion',
  commodity: 'comm',
  comm: 'comm',
  fmp: 'fmp',
  fmp_alpaca: 'fmp_alpaca',
  forex: 'forex',
  pa: 'pa',
  indices: 'indices',
  crypto: 'crypto'
};

let pool = null;
let ready = false;

function normalizeScanner(scanner) {
  const key = String(scanner || '').trim().toLowerCase();
  return ALIASES[key] || key;
}

async function getPool() {
  if (pool) return pool;
  if (!process.env.DATABASE_URL) return null;
  const pg = (await import('pg')).default;
  pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 5 });
  return pool;
}

function seedRows() {
  const file = path.join(__dirname, 'scanner-param-seeds.json');
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

export function castParamValue(value, valueType) {
  if (valueType === 'number') return Number(value);
  if (valueType === 'bool') return String(value).toLowerCase() === 'true';
  return String(value);
}

async function ensureReady() {
  const pg = await getPool();
  if (!pg) return false;
  if (ready) return true;
  await pg.query(`
    CREATE TABLE IF NOT EXISTS scanner_params (
      id serial PRIMARY KEY,
      scanner text NOT NULL,
      param text NOT NULL,
      value text NOT NULL,
      value_type text NOT NULL CHECK (value_type IN ('number','bool','string')),
      variant text NOT NULL DEFAULT 'champion',
      updated_at timestamptz NOT NULL DEFAULT now(),
      updated_by text,
      UNIQUE(scanner, param, variant)
    )
  `);
  // NULL means unbounded. Every pre-existing row therefore behaves
  // exactly as it did before bounds existed.
  await pg.query(`ALTER TABLE scanner_params ADD COLUMN IF NOT EXISTS min_value numeric`);
  await pg.query(`ALTER TABLE scanner_params ADD COLUMN IF NOT EXISTS max_value numeric`);
  for (const [p, b] of Object.entries(PARAM_BOUNDS)) {
    await pg.query(
      `UPDATE scanner_params SET min_value=$2, max_value=$3
       WHERE param=$1 AND (min_value IS DISTINCT FROM $2 OR max_value IS DISTINCT FROM $3)`,
      [p, b.min ?? null, b.max ?? null]);
  }
  await pg.query(`
    CREATE TABLE IF NOT EXISTS intelligence_changelog (
      id serial PRIMARY KEY,
      date timestamptz NOT NULL DEFAULT now(),
      scanner text,
      parameter text,
      old_value text,
      new_value text,
      reason text,
      approved_by text
    )
  `);
  await pg.query(`
    CREATE TABLE IF NOT EXISTS stamp_violations (
      id bigserial PRIMARY KEY, trade_id text, deal_id text, scanner text,
      field text NOT NULL, old_value text, attempted_value text,
      payload jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now()
    );
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS config_hash text;
    ALTER TABLE signals ADD COLUMN IF NOT EXISTS config_hash text;
  `);
  for (const r of seedRows()) {
    await pg.query(`
      INSERT INTO scanner_params(scanner,param,value,value_type,variant,updated_by)
      VALUES($1,$2,$3,$4,'champion','seed-v6-config')
      ON CONFLICT(scanner,param,variant) DO NOTHING
    `, [normalizeScanner(r.scanner), r.param, String(r.value), r.value_type]);
  }
  for (const scanner of SCANNER_ORDER) {
    await pg.query(`
      INSERT INTO scanner_params(scanner,param,value,value_type,variant,updated_by)
      VALUES($1,'BRAIN_VETO_ENABLED','false','bool','champion','seed-brain-shadow')
      ON CONFLICT(scanner,param,variant) DO NOTHING
    `, [scanner]);
    for (const [param,value] of [['MAX_ORDERS_PER_DAY','3'],['MAX_DAILY_LOSS_R','3'],['CONSECUTIVE_LOSS_HALT','5']]) {
      await pg.query(`INSERT INTO scanner_params(scanner,param,value,value_type,variant,updated_by)
        VALUES($1,$2,$3,'number','champion','seed-risk-breakers') ON CONFLICT(scanner,param,variant) DO NOTHING`,[scanner,param,value]);
    }
    const gap= scanner==='indices'?'1.5':scanner==='comm'?'1.5':scanner==='forex'?'1.2':scanner==='crypto'?'1.3':'2.0';
    await pg.query(`INSERT INTO scanner_params(scanner,param,value,value_type,variant,updated_by) VALUES($1,'GAP_MULT',$2,'number','champion','seed-gap-risk') ON CONFLICT(scanner,param,variant) DO NOTHING`,[scanner,gap]);
  }
  ready = true;
  return true;
}

export async function initParamStore() {
  return ensureReady();
}

export async function getScannerParams(scanner, variant = 'champion') {
  const ok = await ensureReady();
  if (!ok) return { params: {}, params_updated_at: null, rows: [] };
  const pg = await getPool();
  const s = normalizeScanner(scanner);
  const result = await pg.query(`
    SELECT scanner,param,value,value_type,variant,updated_at,updated_by
    FROM scanner_params
    WHERE scanner=$1 AND variant=$2
    ORDER BY param
  `, [s, variant]);
  const params = {};
  let latest = null;
  for (const row of result.rows) {
    params[row.param] = castParamValue(row.value, row.value_type);
    if (!latest || new Date(row.updated_at) > new Date(latest)) latest = row.updated_at;
  }
  return { params, params_updated_at: latest, rows: result.rows };
}

export async function recordConfigChangelog({scanner='all',parameter,old_value,new_value,reason,approved_by='admin-api'}) {
  const ok=await ensureReady(); if(!ok)throw new Error('parameter store unavailable');
  const pg=await getPool();
  return (await pg.query(`INSERT INTO intelligence_changelog(scanner,parameter,old_value,new_value,reason,approved_by) VALUES($1,$2,$3,$4,$5,$6) RETURNING *`,[scanner,parameter,String(old_value),String(new_value),reason,approved_by])).rows[0];
}

export async function getConfigHash(scanner, globalRisk = {}) {
  const merged = await getScannerParams(scanner, 'champion');
  const risk = Object.fromEntries(Object.entries(globalRisk || {}).filter(([k]) => /risk|heat|loss|max_open|account_size|paper_only|kill_switch/i.test(k)));
  return crypto.createHash('sha256').update(JSON.stringify(stable({ scanner: normalizeScanner(scanner), params: merged.params, global_risk: risk }))).digest('hex');
}

export async function recordStampViolation({ trade, field, attemptedValue, payload }) {
  const ok = await ensureReady();
  if (!ok) return null;
  const pg = await getPool();
  const client = await pg.connect();
  try {
    await client.query('BEGIN');
    const prior = await client.query(`SELECT count(*)::int n FROM stamp_violations WHERE created_at >= date_trunc('day',now())`);
    const { rows } = await client.query(`INSERT INTO stamp_violations(trade_id,deal_id,scanner,field,old_value,attempted_value,payload)
      VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *`, [String(trade?.id || ''), trade?.deal_id || null, trade?.scanner || null, field, String(trade?.[field] ?? ''), String(attemptedValue ?? ''), payload || {}]);
    await client.query('COMMIT');
    return { ...rows[0], alert_needed: Number(prior.rows[0]?.n || 0) === 0 };
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally { client.release(); }
}

// Ceilings are stated with their justification. Anything that cannot be
// justified is left out rather than guessed: a wrong ceiling is a future
// outage on the day someone legitimately needs to exceed it.
export const PARAM_BOUNDS = {
  // 2% per trade is the outer limit this book is sized for; 0.02 currently
  // passes, so this is a real tightening rather than a codification.
  RISK_PCT:            { min: 0.0001, max: 0.02 },
  // twice the current 10. Beyond that the heat and concentration maths
  // stop being meaningful rather than merely aggressive.
  MAX_OPEN_POSITIONS:  { min: 1, max: 20 },
  // far above the current 3, but still bounds a runaway ordering loop.
  MAX_ORDERS_PER_DAY:  { min: 1, max: 50 },
  // VIX has never printed above 100; a larger value disables the halt
  // silently rather than raising it.
  VIX_HALT:            { min: 1, max: 100 },
  // heat is a percentage of the book. Above 100% is not a stricter
  // setting, it is a meaningless one.
  MAX_GLOBAL_HEAT_PCT: { min: 1, max: 100 }
};

// Above this, RISK_PCT needs an explicit confirm flag and raises a
// Telegram. It is a speed bump, not a ceiling -- the ceiling is max.
export const PARAM_SOFT_CONFIRM = { RISK_PCT: 0.01 };

// Lets callers outside the param store write an attributed changelog row.
// Failure to log must never block the operation it is recording.
export async function logChangelogRow({ scanner, parameter, old_value, new_value, reason, approved_by }) {
  try {
    const pg = await getPool();
    if (!pg) return false;
    await pg.query(
      `INSERT INTO intelligence_changelog(scanner, parameter, old_value, new_value, reason, approved_by)
       VALUES($1,$2,$3,$4,$5,$6)`,
      [scanner, parameter, old_value === null || old_value === undefined ? null : String(old_value),
       new_value === null || new_value === undefined ? null : String(new_value),
       String(reason || ''), approved_by || 'unknown']);
    return true;
  } catch { return false; }
}

export function validateParamValue(row, rawValue, opts = {}) {
  const value = String(rawValue);
  if (!value.length) return { ok: false, error: 'value required' };
  if (row.value_type === 'bool') {
    if (!/^(true|false)$/i.test(value)) return { ok: false, error: 'boolean value must be true or false' };
    return { ok: true, value: value.toLowerCase() };
  }
  if (row.value_type === 'number') {
    const n = Number(value);
    if (!Number.isFinite(n)) return { ok: false, error: 'numeric value must parse as a finite number' };
    const p = row.param.toUpperCase();
    const allowNegative = /PULLBACK|CRASH|LOSS_LIMIT/.test(p) && !/RISK|HEAT/.test(p);
    if (!allowNegative && n < 0) return { ok: false, error: 'negative values are not allowed for this parameter' };
    const mustBePositive = new Set(['GAP_MULT','CONSECUTIVE_LOSS_HALT','VIX_HALT']).has(p) || /RISK|HEAT|LOOKBACK|LIMIT|WINDOW|BARS|MINUTES|HOURS|MAX_|MIN_|SCORE|SIZE|PRICE|VOLUME|VOL|RRR|RATIO|ATR|PIPS|LOTS|POSITIONS|TRADES|COOLDOWN|LEVERAGE|MARGIN|TP|SL|STOP/.test(p);
    if (mustBePositive && n <= 0) return { ok: false, error: 'zero or negative values would silently break this parameter' };
    // Bounds come from the ROW, so a NULL column is unbounded and the
    // pre-existing behaviour is preserved exactly.
    const lo = row.min_value === null || row.min_value === undefined ? null : Number(row.min_value);
    const hi = row.max_value === null || row.max_value === undefined ? null : Number(row.max_value);
    if (lo !== null && Number.isFinite(lo) && n < lo) {
      return { ok: false, error: `value ${n} is below the minimum ${lo} for ${p}`, bound: { min: lo, max: hi } };
    }
    if (hi !== null && Number.isFinite(hi) && n > hi) {
      return { ok: false, error: `value ${n} exceeds the maximum ${hi} for ${p}`, bound: { min: lo, max: hi } };
    }
    const soft = PARAM_SOFT_CONFIRM[p];
    if (soft !== undefined && n > soft && opts.confirm !== true) {
      return { ok: false, requires_confirm: true, soft_limit: soft,
               error: `${p}=${n} exceeds the ${soft} soft limit; resend with confirm:true to proceed`,
               bound: { min: lo, max: hi } };
    }
    return { ok: true, value: String(n), confirmed_above_soft_limit: soft !== undefined && n > soft };
  }
  return { ok: true, value };
}

function eventTime(row) {
  const ts = row.created_at || row.closed_at || row.opened_at || row.ts || row.timestamp || row.data?.ts || row.data?.timestamp;
  const d = ts ? new Date(ts) : null;
  return d && Number.isFinite(d.getTime()) ? d : null;
}

function ageMinutes(date) {
  if (!date) return null;
  return Math.round((Date.now() - date.getTime()) / 60000);
}

function scannerOf(row) {
  return normalizeScanner(row.scanner || row.data?.scanner);
}

function topReason(rejections, scanner) {
  const weekAgo = Date.now() - 7 * 86400000;
  const counts = new Map();
  for (const r of rejections || []) {
    if (scannerOf(r) !== scanner) continue;
    const d = eventTime(r);
    if (!d || d.getTime() < weekAgo) continue;
    const reason = r.reason || r.data?.reason || 'UNKNOWN';
    counts.set(reason, (counts.get(reason) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0] || null;
}

function expectancy(trades, scanner) {
  const closed = (trades || [])
    .filter(t => scannerOf(t) === scanner && String(t.status || t.data?.status || '').toUpperCase() === 'CLOSED')
    .sort((a, b) => (eventTime(b)?.getTime() || 0) - (eventTime(a)?.getTime() || 0))
    .slice(0, 30);
  if (closed.length < 30) return { status: 'insufficient', count: closed.length, too_thin: true };
  const pnl = closed.map(t => Number(t.pnl_net ?? t.data?.pnl_net ?? t.pnl ?? t.data?.pnl ?? 0));
  const avg = pnl.reduce((s, n) => s + n, 0) / pnl.length;
  const wins = pnl.filter(n => n > 0).length;
  return { status: 'ok', count: closed.length, too_thin: false, expectancy: +avg.toFixed(2), win_rate_pct: +(wins / pnl.length * 100).toFixed(1) };
}

async function scorecardRows(db) {
  await ensureReady();
  const pg = await getPool();
  const topFilterVerdicts = await getTopFilterVerdictsByScanner().catch(() => ({}));
  const changeMap = pg ? Object.fromEntries((await pg.query(`SELECT DISTINCT ON(scanner) scanner,parameter,status,verdict,n_after FROM change_evaluations ORDER BY scanner,changed_at DESC`)).rows.map(r=>[r.scanner,r])) : {};
  const challengerMap = pg ? Object.fromEntries((await pg.query(`SELECT DISTINCT ON(scanner) scanner,verdict,n_disagreements,computed_at FROM challenger_evals ORDER BY scanner,computed_at DESC`)).rows.map(r=>[r.scanner,r])) : {};
  const latencyMap = Object.fromEntries((await latencyStats().catch(()=>[])).map(r=>[r.scanner,r]));
  const validationMap = pg ? Object.fromEntries((await pg.query(`WITH good AS (
    SELECT scanner,count(*)::int n,count(*) filter(where price_validated)::int validated,
      avg(price_divergence_pct) filter(where price_validated) avg_divergence_pct
    FROM signal_outcomes o WHERE labeled_at>=now()-interval '30 days' AND ${analyticsFirewallSql('o')} GROUP BY scanner
  ), bad AS (
    SELECT scanner,count(*)::int divergent FROM outcome_label_skips
    WHERE reason='bad_tick_suspected' AND last_attempt_at>=now()-interval '30 days' GROUP BY scanner
  ) SELECT coalesce(g.scanner,b.scanner) scanner,coalesce(g.n,0)::int n,coalesce(g.validated,0)::int validated,
    coalesce(b.divergent,0)::int divergent,g.avg_divergence_pct FROM good g FULL JOIN bad b USING(scanner)`)).rows.map(r=>[r.scanner,{...r,divergence_rate_pct:(Number(r.validated)+Number(r.divergent))?+(100*Number(r.divergent)/(Number(r.validated)+Number(r.divergent))).toFixed(2):null,too_thin:(Number(r.validated)+Number(r.divergent))<30,primary_source:'fmp',secondary_source:'yahoo'}])) : {};
  const paramRows = pg ? (await pg.query(`
    SELECT p.*, c.date AS last_change_date, c.old_value, c.new_value, c.reason AS change_reason
    FROM scanner_params p
    LEFT JOIN LATERAL (
      SELECT date, old_value, new_value, reason
      FROM intelligence_changelog c
      WHERE c.scanner=p.scanner AND c.parameter=p.param
      ORDER BY date DESC LIMIT 1
    ) c ON true
    WHERE p.variant='champion'
    ORDER BY p.scanner,p.param
  `)).rows : [];
  const groupedParams = {};
  for (const p of paramRows) {
    groupedParams[p.scanner] = groupedParams[p.scanner] || [];
    groupedParams[p.scanner].push({
      param: p.param,
      value: castParamValue(p.value, p.value_type),
      raw_value: p.value,
      value_type: p.value_type,
      updated_at: p.updated_at,
      last_change_date: p.last_change_date,
      last_change: p.last_change_date ? {
        date: p.last_change_date,
        old_value: p.old_value,
        new_value: p.new_value,
        reason: p.change_reason
      } : null
    });
  }
  const hb = db.data.heartbeats || {};
  const outputMap=pg?Object.fromEntries((await pg.query(`WITH x AS (
    SELECT scanner,'signal' kind,max(NULLIF(data->>'ts','')::timestamptz) ts FROM signals GROUP BY scanner
    UNION ALL SELECT scanner,'rejection',max(COALESCE(NULLIF(data->>'rejected_time',''),NULLIF(data->>'ts',''))::timestamptz) FROM rejections r WHERE ${analyticsFirewallSql('','r')} GROUP BY scanner
    UNION ALL SELECT scanner,'trade',max(opened_at) FROM trades GROUP BY scanner)
    SELECT scanner,jsonb_object_agg(kind,ts) times FROM x GROUP BY scanner`)).rows.map(r=>[r.scanner,r.times])):{};
  const topReasonMap=pg?Object.fromEntries((await pg.query(`SELECT DISTINCT ON(scanner) scanner,reason,n FROM (
    SELECT scanner,COALESCE(data->>'reason','UNKNOWN') reason,count(*)::int n FROM rejections r
    WHERE created_at>=now()-interval '7 days' AND ${analyticsFirewallSql('','r')} GROUP BY scanner,COALESCE(data->>'reason','UNKNOWN')) x ORDER BY scanner,n DESC`)).rows.map(r=>[r.scanner,[r.reason,r.n]])):{};
  return SCANNER_ORDER.map(scanner => {
    const heartbeat = hb[scanner] || null;
    const outputs=outputMap[scanner]||{};
    const reason = topReasonMap[scanner]||null;
    const params = groupedParams[scanner] || [];
    const lastChange = params.map(p => p.last_change).filter(Boolean).sort((a, b) => new Date(b.date) - new Date(a.date))[0] || null;
    return {
      scanner,
      heartbeat: heartbeat ? {
        ...heartbeat,
        age_minutes: ageMinutes(new Date(Number(heartbeat.ts) || heartbeat.iso || heartbeat.ts)),
        state: ageMinutes(new Date(Number(heartbeat.ts) || heartbeat.iso || heartbeat.ts)) <= 30 ? 'ALIVE' : 'STALE'
      } : { state: 'MISSING', age_minutes: null },
      productivity: {
        last_signal_age_minutes: ageMinutes(outputs.signal?new Date(outputs.signal):null),
        last_trade_age_minutes: ageMinutes(outputs.trade?new Date(outputs.trade):null),
        last_rejection_age_minutes: ageMinutes(outputs.rejection?new Date(outputs.rejection):null)
      },
      expectancy_30: expectancy(db.data.trades || [], scanner),
      top_rejection_reason_7d: reason ? { reason: reason[0], count: reason[1] } : null,
      top_filter_verdict: topFilterVerdicts[scanner] ? { ...topFilterVerdicts[scanner], too_thin:Number(topFilterVerdicts[scanner].n_labeled||0)<30 } : null,
      latest_change_evaluation: changeMap[scanner] || null,
      latest_challenger: challengerMap[scanner] || null,
      latency_24h: latencyMap[scanner] || { n:0,p50_ms:null,p90_ms:null,too_thin:true },
      price_validation_30d: validationMap[scanner] || { n:0,validated:0,divergent:0,divergence_rate_pct:null,too_thin:true },
      params_source: params.length ? 'server' : 'local',
      params,
      last_param_change: lastChange
    };
  });
}

export function attachParamStore(app, db, { adminOnly, journalEvent, sendTelegramAlert }) {
  app.get('/api/params/bounds', adminOnly, async (req, res) => {
    try {
      const ok = await ensureReady();
      if (!ok) return res.status(503).json({ error: 'parameter store unavailable' });
      const pg = await getPool();
      const { rows } = await pg.query(
        `SELECT param, min_value, max_value, count(*)::int AS rows
         FROM scanner_params WHERE min_value IS NOT NULL OR max_value IS NOT NULL
         GROUP BY param, min_value, max_value ORDER BY param`);
      res.json({ bounds: rows, soft_confirm: PARAM_SOFT_CONFIRM, unbounded_means: 'NULL' });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.post('/api/params', adminOnly, async (req, res) => {
    try {
      const { scanner, param, value, reason } = req.body || {};
      const s = normalizeScanner(scanner);
      const p = String(param || '').trim();
      if (!s || !p) return res.status(400).json({ error: 'scanner and param required' });
      if (!String(reason || '').trim()) return res.status(400).json({ error: 'reason required' });
      const ok = await ensureReady();
      if (!ok) return res.status(503).json({ error: 'parameter store unavailable' });
      const pg = await getPool();
      const found = await pg.query(`SELECT * FROM scanner_params WHERE scanner=$1 AND param=$2 AND variant='champion'`, [s, p]);
      if (!found.rows.length) return res.status(400).json({ error: 'unknown scanner parameter', scanner: s, param: p });
      const row = found.rows[0];
      const validation = validateParamValue(row, value, { confirm: req.body?.confirm === true });
      if (!validation.ok) {
        // A silently dropped bad write is worse than a loud failure.
        await pg.query(
          `INSERT INTO intelligence_changelog(scanner, parameter, old_value, new_value, reason, approved_by)
           VALUES($1,$2,$3,$4,$5,'admin-api-rejected')`,
          [s, `REJECTED:${p}`, row.value, String(value), validation.error]).catch(() => {});
        return res.status(400).json({ error: validation.error, scanner: s, param: p,
          requires_confirm: validation.requires_confirm === true, bound: validation.bound || null });
      }
      if (validation.confirmed_above_soft_limit) {
        sendTelegramAlert?.(`PARAM ABOVE SOFT LIMIT\nscanner: ${s}\nparam: ${p}\nold: ${row.value}\nnew: ${validation.value}`);
      }
      const beforeHash = await getConfigHash(s, db.data.scanner_config || {});
      await pg.query('BEGIN');
      try {
        await pg.query(`
          UPDATE scanner_params
          SET value=$1, updated_at=now(), updated_by='admin-api'
          WHERE scanner=$2 AND param=$3 AND variant='champion'
        `, [validation.value, s, p]);
        const log = await pg.query(`
          INSERT INTO intelligence_changelog(scanner, parameter, old_value, new_value, reason, approved_by)
          VALUES($1,$2,$3,$4,$5,'admin-api')
          RETURNING *
        `, [s, p, row.value, validation.value, String(reason).trim()]);
        await pg.query('COMMIT');
        const afterHash = await getConfigHash(s, db.data.scanner_config || {});
        const evaluation = await createChangeEvaluation(log.rows[0], beforeHash, afterHash);
        await journalEvent?.('param_change', { scanner:s, payload:{ ...log.rows[0], config_hash_before:beforeHash, config_hash_after:afterHash } });
        res.json({ status: 'ok', scanner: s, param: p, old_value: row.value, new_value: validation.value, config_hash_before:beforeHash, config_hash_after:afterHash, changelog: log.rows[0], evaluation });
      } catch (e) {
        await pg.query('ROLLBACK');
        throw e;
      }
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/scorecard', adminOnly, async (req, res) => {
    try {
      res.json({ rows: await scorecardRows(db), generated_at: new Date().toISOString() });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/scorecard/block/:name', adminOnly, async (req, res) => {
    try {
      const allowed = new Set(['heartbeat', 'productivity', 'expectancy_30', 'top_rejection_reason_7d', 'top_filter_verdict', 'params', 'latest_change_evaluation', 'latest_challenger', 'latency_24h', 'price_validation_30d']);
      const name = String(req.params.name || '');
      if (!allowed.has(name)) return res.status(404).json({ error: 'unknown scorecard block' });
      const rows = (await scorecardRows(db)).map(row => ({ scanner: row.scanner, value: name === 'params' ? { params: row.params, params_source: row.params_source, last_param_change: row.last_param_change } : row[name] }));
      res.json({ block: name, rows, generated_at: new Date().toISOString() });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, stable(value[k])]));
  return value;
}

export { SCANNER_ORDER };
