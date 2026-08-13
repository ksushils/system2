import pg from 'pg';
import { analyticsFirewallSql } from './analytics-firewall.js';

const { Pool } = pg;
const DATABASE_URL = process.env.DATABASE_URL || '';
const pool = DATABASE_URL ? new Pool({ connectionString: DATABASE_URL, max: Number(process.env.BRAIN_POOL_MAX || 2), application_name: 'fund-system-brain' }) : null;

function n(v) {
  if (v === null || v === undefined || v === '') return null;
  const x = Number(v);
  return Number.isFinite(x) ? x : null;
}

function text(v, fallback = '') {
  const s = String(v ?? '').trim();
  return s || fallback;
}

function scannerTag(v) {
  return text(v).toLowerCase();
}

function setupType(data = {}) {
  return text(data.setup_type || data.trade_type || data.strategy || data.setup || data.pattern, 'UNKNOWN').toUpperCase();
}

function directionOf(data = {}) {
  const d = text(data.direction || data.side || data.type || data.action, 'LONG').toUpperCase();
  if (['SELL', 'SHORT'].includes(d)) return 'SHORT';
  return 'LONG';
}

function bucket(value, step, maxBucket = 999) {
  const x = n(value);
  if (x === null) return null;
  return Math.max(-maxBucket, Math.min(maxBucket, Math.round(x / step)));
}

function volumeBucket(value) {
  const x = n(value);
  if (x === null) return null;
  if (x < 0.75) return 0;
  if (x < 1.25) return 1;
  if (x < 2) return 2;
  if (x < 3) return 3;
  return 4;
}

function scoreBucket(value) {
  const x = n(value);
  if (x === null) return null;
  return Math.floor(x / 2);
}

function vixBucket(value) {
  const x = n(value);
  if (x === null) return null;
  if (x < 15) return 0;
  if (x < 22) return 1;
  return 2;
}

function eventDateParts(eventTs) {
  const d = eventTs ? new Date(eventTs) : new Date();
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: 'numeric',
    weekday: 'short',
    hour12: false
  }).formatToParts(d);
  const obj = Object.fromEntries(parts.map(p => [p.type, p.value]));
  const dowMap = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
  return { hour_et: Number(obj.hour), dow: dowMap[obj.weekday] ?? null };
}

export function brainFeatureVector(data = {}, eventTs = null) {
  const parts = eventDateParts(eventTs || data.ts || data.rejected_time || data.timestamp);
  return {
    scanner: scannerTag(data.scanner),
    setup_type: setupType(data),
    direction: directionOf(data),
    hour_et: parts.hour_et,
    dow: parts.dow,
    rsi_bucket: bucket(data.rsi, 10),
    volume_bucket: volumeBucket(data.volume_ratio ?? data.vol_ratio ?? data.relative_volume),
    score_bucket: scoreBucket(data.score ?? data.quality_score ?? data.signal_score),
    spy_trend: text(data.spy_trend || data.spy_regime || data.regime, 'UNKNOWN').toUpperCase(),
    vix_tercile: vixBucket(data.vix_level ?? data.vix),
    config_hash: text(data.config_hash) || null
  };
}

function distPart(a, b, weight = 1) {
  if (a === null || a === undefined || b === null || b === undefined) return { d: 0, w: 0 };
  if (typeof a === 'number' && typeof b === 'number') return { d: Math.min(3, Math.abs(a - b)) * weight, w: 3 * weight };
  return { d: a === b ? 0 : weight, w: weight };
}

export function brainDistance(a, b) {
  const parts = [
    distPart(a.hour_et, b.hour_et, 1),
    distPart(a.dow, b.dow, 0.75),
    distPart(a.rsi_bucket, b.rsi_bucket, 1.25),
    distPart(a.volume_bucket, b.volume_bucket, 1.25),
    distPart(a.score_bucket, b.score_bucket, 1.5),
    distPart(a.spy_trend, b.spy_trend, 1),
    distPart(a.vix_tercile, b.vix_tercile, 1)
  ];
  const totalW = parts.reduce((s, p) => s + p.w, 0);
  const totalD = parts.reduce((s, p) => s + p.d, 0);
  return totalW ? +(totalD / totalW).toFixed(6) : 999;
}

function poolRowToFeatures(row) {
  const data = row.data || {};
  return brainFeatureVector({
    ...data,
    scanner: row.scanner || data.scanner,
    direction: row.direction || data.direction,
    ticker: row.ticker || data.ticker
  }, row.event_ts);
}

async function neighborPool(queryFeatures, limit = 3000) {
  if (!pool) return [];
  const { rows } = await pool.query(`
    SELECT
      o.source_type,
      o.source_id,
      o.scanner,
      o.ticker,
      o.direction,
      o.event_ts,
      o.ret_1d::float8 AS ret_1d,
      o.ret_3d::float8 AS ret_3d,
      o.barrier_outcome,
      o.measurement_population,
      COALESCE(s.data, r.data, '{}'::jsonb) AS data
    FROM signal_outcomes o
    LEFT JOIN signals s ON o.source_type='signal' AND s.id::text=o.source_id
    LEFT JOIN rejections r ON o.source_type='rejection' AND r.id::text=o.source_id
    WHERE o.ret_1d IS NOT NULL
      AND o.scanner=$1
      AND ($3::text IS NULL OR o.config_hash=$3)
      AND ${analyticsFirewallSql('o','r')}
      AND NOT (
        o.source_type='rejection'
        AND (
          COALESCE(r.data->>'reason','') LIKE 'BRAIN_%'
          OR COALESCE(r.data->>'reason','') LIKE 'DATA_QUALITY%'
        )
      )
    ORDER BY o.event_ts DESC
    LIMIT $2
  `, [queryFeatures.scanner, limit, queryFeatures.config_hash || null]);
  return rows
    .map(row => ({ ...row, features: poolRowToFeatures(row) }))
    .filter(row =>
      row.features.scanner === queryFeatures.scanner &&
      row.features.setup_type === queryFeatures.setup_type &&
      row.features.direction === queryFeatures.direction
    );
}

function summarizeNeighbors(neighbors) {
  const nCount = neighbors.length;
  const wins = neighbors.filter(row => Number(row.ret_1d) > 0).length;
  const avg = nCount ? neighbors.reduce((s, row) => s + Number(row.ret_1d || 0), 0) / nCount : null;
  const barrierRows = neighbors.filter(row => row.measurement_population === 'ENTERED' && (row.barrier_outcome === 'TP_FIRST' || row.barrier_outcome === 'SL_FIRST'));
  const tpFirst = barrierRows.filter(row => row.barrier_outcome === 'TP_FIRST').length;
  let verdict = 'INSUFFICIENT';
  const pctWon = nCount ? +(wins / nCount * 100).toFixed(1) : null;
  const avgRet = avg === null ? null : +avg.toFixed(6);
  const barrierRate=barrierRows.length ? +(tpFirst / barrierRows.length * 100).toFixed(1) : null;
  if (nCount >= 20) {
    verdict = pctWon < 35 && avgRet < 0 && (barrierRate===null || barrierRate<35) ? 'VETO' : 'PASS';
  }
  return {
    verdict,
    n: nCount,
    pct_won: pctWon,
    avg_ret_1d: avgRet,
    barrier_tp_vs_sl_rate: barrierRate,
    sample_ids: neighbors.slice(0, 5).map(row => `${row.source_type}:${row.source_id}`)
  };
}

export function brainStatsText(stats) {
  return `n=${stats.n}, pct_won=${stats.pct_won ?? 'NA'}%, avg_ret_1d=${stats.avg_ret_1d ?? 'NA'}, tp_vs_sl=${stats.barrier_tp_vs_sl_rate ?? 'NA'}%`;
}

export async function similarOutcomeVerdict(candidate = {}) {
  const query_features = brainFeatureVector(candidate);
  if (!query_features.scanner) {
    return { verdict: 'INSUFFICIENT', n: 0, pct_won: null, avg_ret_1d: null, barrier_tp_vs_sl_rate: null, sample_ids: [], query_features, distance_function: DISTANCE_DOC };
  }
  const informative=[query_features.rsi_bucket,query_features.volume_bucket,query_features.score_bucket,query_features.vix_tercile,query_features.spy_trend==='UNKNOWN'?null:query_features.spy_trend].filter(v=>v!==null&&v!==undefined).length;
  if(informative<3)return {verdict:'INSUFFICIENT',reason:'candidate lacks conditioning features',n:0,pct_won:null,avg_ret_1d:null,barrier_tp_vs_sl_rate:null,sample_ids:[],entered:{n:0},never_entered_advisory:{n:0},query_features,distance_function:DISTANCE_DOC};
  const poolRows = await neighborPool(query_features);
  const scored = poolRows
    .map(row => ({ ...row, distance: brainDistance(query_features, row.features) }))
    .sort((a, b) => a.distance - b.distance || new Date(b.event_ts) - new Date(a.event_ts))
    .slice(0, 50);
  const entered=scored.filter(r=>r.measurement_population==='ENTERED');
  const neverEntered=scored.filter(r=>r.measurement_population!=='ENTERED');
  const enteredSummary=summarizeNeighbors(entered);
  const advisory=summarizeNeighbors(neverEntered);
  if(entered.length<20){enteredSummary.verdict='INSUFFICIENT';enteredSummary.reason='fewer than 20 ENTERED neighbours';}
  return {
    ...enteredSummary,
    entered:enteredSummary,
    never_entered_advisory:{...advisory,verdict:'ADVISORY'},
    query_features,
    distance_function: DISTANCE_DOC
  };
}

export const DISTANCE_DOC = 'Neighbors must match scanner + setup_type + direction exactly. Among that pool, distance is weighted normalized bucket distance across hour_et (1), day_of_week (0.75), rsi_bucket by 10s (1.25), volume bucket (1.25), score bucket by 2s (1.5), spy_trend (1), and vix tercile (1). Null fields carry zero weight.';

export async function brainShadowStats() {
  if (!pool) throw new Error('Postgres is required for brain stats');
  const { rows } = await pool.query(`
    WITH shadow AS (
      SELECT r.id::text rejection_id, r.scanner, r.ticker, r.data, o.ret_1d::float8, o.ret_3d::float8
      FROM rejections r
      LEFT JOIN signal_outcomes o ON o.source_type='rejection' AND o.source_id=r.id::text
      WHERE COALESCE(r.data->>'reason','') LIKE 'BRAIN_SHADOW_VETO:%' AND ${analyticsFirewallSql('o','r')}
    )
    SELECT
      count(*)::int AS total_shadow_vetoes,
      count(*) FILTER (WHERE ret_1d IS NOT NULL)::int AS labeled,
      count(*) FILTER (WHERE ret_1d < 0)::int AS actually_lost,
      round((100.0 * count(*) FILTER (WHERE ret_1d < 0) / NULLIF(count(*) FILTER (WHERE ret_1d IS NOT NULL),0))::numeric, 2) AS pct_actually_lost,
      round(avg(ret_1d)::numeric, 6) AS avg_ret_1d
    FROM shadow
  `);
  const byScanner = await pool.query(`
    SELECT r.scanner,
      count(*)::int AS total_shadow_vetoes,
      count(o.ret_1d)::int AS labeled,
      count(*) FILTER (WHERE o.ret_1d < 0)::int AS actually_lost,
      round(avg(o.ret_1d)::numeric, 6) AS avg_ret_1d
    FROM rejections r
    LEFT JOIN signal_outcomes o ON o.source_type='rejection' AND o.source_id=r.id::text
    WHERE COALESCE(r.data->>'reason','') LIKE 'BRAIN_SHADOW_VETO:%' AND ${analyticsFirewallSql('o','r')}
    GROUP BY r.scanner
    ORDER BY total_shadow_vetoes DESC, r.scanner
  `);
  return { overall: rows[0], by_scanner: byScanner.rows };
}
