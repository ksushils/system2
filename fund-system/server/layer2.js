import pg from 'pg';
import { analyticsFirewallSql } from './analytics-firewall.js';

const { Pool } = pg;
const DATABASE_URL = process.env.DATABASE_URL || '';
const pool = DATABASE_URL ? new Pool({ connectionString: DATABASE_URL, max: Number(process.env.LAYER2_POOL_MAX || 2), application_name: 'fund-system-layer2' }) : null;

let weeklyStarted = false;
let lastWeeklyRunDate = '';

const VALID_VERDICTS = new Set(['SAVING_YOU', 'COSTING_YOU', 'NO_EFFECT', 'INSUFFICIENT_DATA']);

function normalizeScanner(v) {
  return String(v || '').trim().toLowerCase();
}

function verdictFor({ nLabeled, avgRet1d, pctWouldHaveWon }) {
  if (nLabeled < 30) return 'INSUFFICIENT_DATA';
  if (avgRet1d <= -0.002) return 'SAVING_YOU';
  if (avgRet1d >= 0.003 && pctWouldHaveWon >= 55) return 'COSTING_YOU';
  return 'NO_EFFECT';
}

async function ensureReady() {
  if (!pool) return false;
  await pool.query(`
    CREATE TABLE IF NOT EXISTS filter_verdicts (
      id BIGSERIAL PRIMARY KEY,
      computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      scanner TEXT,
      reason TEXT,
      window_days INTEGER NOT NULL,
      n_rejections INTEGER NOT NULL DEFAULT 0,
      n_labeled INTEGER NOT NULL DEFAULT 0,
      avg_ret_1d NUMERIC,
      avg_ret_3d NUMERIC,
      pct_would_have_won NUMERIC,
      verdict TEXT NOT NULL CHECK (verdict IN ('SAVING_YOU','COSTING_YOU','NO_EFFECT','INSUFFICIENT_DATA')),
      est_missed_pnl_r NUMERIC,
      dq_flag TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_filter_verdicts_latest ON filter_verdicts(computed_at DESC, scanner, verdict);
  `);
  await pool.query('ALTER TABLE filter_verdicts ADD COLUMN IF NOT EXISTS config_hash text');
  await pool.query(`ALTER TABLE filter_verdicts ADD COLUMN IF NOT EXISTS measurement_basis text;
    ALTER TABLE filter_verdicts ADD COLUMN IF NOT EXISTS n_entered integer DEFAULT 0;
    ALTER TABLE filter_verdicts ADD COLUMN IF NOT EXISTS n_never_entered integer DEFAULT 0;`);
  await pool.query(`ALTER TABLE filter_verdicts ADD COLUMN IF NOT EXISTS est_missed_r_upper_bound numeric`);
  return true;
}

export async function initLayer2() {
  return ensureReady();
}

async function latestComputedAt() {
  const { rows } = await pool.query('SELECT max(computed_at) AS computed_at FROM filter_verdicts');
  return rows[0]?.computed_at || null;
}

export async function runFilterVerdicts({ windowDays = 30, crossEra = false } = {}) {
  if (!pool) throw new Error('Postgres is required for filter verdicts');
  await ensureReady();
  const computedAt = new Date();
  const { rows } = await pool.query(`
    WITH labeled AS (
      SELECT
        COALESCE(o.scanner,r.scanner) scanner,
        COALESCE(NULLIF(r.data->>'reason',''), NULLIF(r.data->>'rejection_reason',''), NULLIF(r.data->>'message',''), 'UNKNOWN') AS reason,
        o.source_id,
        COALESCE(o.ticker,r.ticker,r.data->>'ticker') ticker,
        COALESCE(o.event_ts,NULLIF(r.data->>'rejected_time','')::timestamptz,NULLIF(r.data->>'ts','')::timestamptz) event_ts,
        o.ret_1d::numeric AS ret_1d,
        o.ret_3d::numeric AS ret_3d,
        o.config_hash,
        COALESCE(NULLIF(r.data->>'intended_entry','')::numeric,NULLIF(r.data->>'rejected_price','')::numeric) intended_entry,
        COALESCE(NULLIF(r.data->>'intended_sl','')::numeric,NULLIF(r.data->>'sl','')::numeric) initial_sl,
        COALESCE(o.measurement_population,'NEVER_ENTERED') measurement_population,
        CASE
          WHEN COALESCE(o.scanner,r.scanner)='mean_reversion'
           AND COALESCE(o.event_ts,NULLIF(r.data->>'rejected_time','')::timestamptz,NULLIF(r.data->>'ts','')::timestamptz) >= '2026-07-28T00:00:00Z'::timestamptz
           AND COALESCE(o.event_ts,NULLIF(r.data->>'rejected_time','')::timestamptz,NULLIF(r.data->>'ts','')::timestamptz) <  '2026-08-04T00:00:00Z'::timestamptz
          THEN 'MR_DOUBLED'
          ELSE NULL
        END AS dq_flag
      FROM rejections r
      LEFT JOIN signal_outcomes o ON o.source_type='rejection' AND r.id::text = o.source_id
      WHERE COALESCE(o.event_ts,NULLIF(r.data->>'rejected_time','')::timestamptz,NULLIF(r.data->>'ts','')::timestamptz) >= $2::timestamptz - ($1 || ' days')::interval
        AND COALESCE(o.event_ts,NULLIF(r.data->>'rejected_time','')::timestamptz,NULLIF(r.data->>'ts','')::timestamptz) < $2::timestamptz
        AND ($3::boolean OR o.config_hash IS NULL OR o.config_hash = (SELECT x.config_hash FROM signal_outcomes x WHERE x.scanner=o.scanner AND x.config_hash IS NOT NULL ORDER BY x.event_ts DESC LIMIT 1))
        AND ${analyticsFirewallSql('o','r')}
    ),
    grouped AS (
      SELECT
        scanner,
        reason,
        count(*) FILTER (WHERE ret_1d IS NOT NULL)::int AS n_labeled,
        count(*)::int AS n_rejections,
        avg(ret_1d) FILTER (WHERE ret_1d IS NOT NULL) AS avg_ret_1d,
        avg(ret_3d) AS avg_ret_3d,
        (100.0 * avg(CASE WHEN ret_1d > 0 THEN 1.0 ELSE 0.0 END) FILTER (WHERE ret_1d IS NOT NULL)) AS pct_would_have_won,
        percentile_cont(.5) WITHIN GROUP (ORDER BY abs(intended_entry-initial_sl)/NULLIF(abs(intended_entry),0)) FILTER (WHERE intended_entry IS NOT NULL AND initial_sl IS NOT NULL AND intended_entry<>0) median_risk_pct,
        count(*) FILTER (WHERE measurement_population='ENTERED')::int n_entered,
        count(*) FILTER (WHERE measurement_population='NEVER_ENTERED')::int n_never_entered,
        CASE WHEN bool_or(dq_flag='MR_DOUBLED') THEN 'MR_DOUBLED' ELSE NULL END AS dq_flag
      FROM labeled
      GROUP BY scanner, reason
    )
    SELECT * FROM grouped
    ORDER BY scanner, n_labeled DESC, reason
  `, [windowDays, computedAt, crossEra]);

  const inserted = [];
  for (const row of rows) {
    const nLabeled = Number(row.n_labeled) || 0;
    const avgRet1d = Number(row.avg_ret_1d) || 0;
    const pctWouldHaveWon = Number(row.pct_would_have_won) || 0;
    const verdict = verdictFor({ nLabeled, avgRet1d, pctWouldHaveWon });
    if (!VALID_VERDICTS.has(verdict)) throw new Error(`invalid verdict ${verdict}`);
    const estMissed = verdict === 'COSTING_YOU' && Number(row.median_risk_pct)>0 ? Number(row.avg_ret_1d||0)*Number(row.n_labeled||0)/Number(row.median_risk_pct) : null;
    const result = await pool.query(`
      INSERT INTO filter_verdicts(
        computed_at, scanner, reason, window_days, n_rejections, n_labeled,
        avg_ret_1d, avg_ret_3d, pct_would_have_won, verdict, est_missed_pnl_r, dq_flag, config_hash,measurement_basis,n_entered,n_never_entered,est_missed_r_upper_bound
      )
      VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NULL,$11,$12,'MARKOUT_RETURNS',$13,$14,$15)
      RETURNING *
    `, [
      computedAt, row.scanner, row.reason, windowDays, row.n_rejections, row.n_labeled,
      row.avg_ret_1d, row.avg_ret_3d, row.pct_would_have_won, verdict,row.dq_flag,crossEra ? 'CROSS_ERA' : null,row.n_entered,row.n_never_entered,estMissed
    ]);
    inserted.push(result.rows[0]);
  }
  console.log('[filter-verdicts]', JSON.stringify({ computed_at: computedAt.toISOString(), window_days: windowDays, verdicts: inserted.length }));
  return { computed_at: computedAt.toISOString(), window_days: windowDays, verdicts: inserted };
}

export async function getLatestFilterVerdicts({ scanner = '' } = {}) {
  if (!pool) throw new Error('Postgres is required for filter verdicts');
  await ensureReady();
  const latest = await latestComputedAt();
  if (!latest) return { computed_at: null, rows: [] };
  const params = [latest];
  let filter = 'computed_at=$1';
  const s = normalizeScanner(scanner);
  if (s) {
    params.push(s);
    filter += ` AND scanner=$${params.length}`;
  }
  const { rows } = await pool.query(`
    SELECT *
    FROM filter_verdicts
    WHERE ${filter}
    ORDER BY
      CASE verdict WHEN 'COSTING_YOU' THEN 0 WHEN 'SAVING_YOU' THEN 1 WHEN 'NO_EFFECT' THEN 2 ELSE 3 END,
      est_missed_r_upper_bound DESC NULLS LAST,
      n_labeled DESC,
      scanner,
      reason
  `, params);
  return { computed_at: latest, rows };
}

export async function getTopFilterVerdictsByScanner() {
  if (!pool) return {};
  await ensureReady();
  const latest = await latestComputedAt();
  if (!latest) return {};
  const { rows } = await pool.query(`
    SELECT DISTINCT ON (scanner)
      scanner, reason, verdict, n_labeled, est_missed_r_upper_bound, dq_flag
    FROM filter_verdicts
    WHERE computed_at=$1
    ORDER BY scanner,
      CASE verdict WHEN 'COSTING_YOU' THEN 0 WHEN 'SAVING_YOU' THEN 1 WHEN 'NO_EFFECT' THEN 2 ELSE 3 END,
      est_missed_r_upper_bound DESC NULLS LAST,
      n_labeled DESC
  `, [latest]);
  return Object.fromEntries(rows.map(r => [r.scanner, r]));
}

export async function firewallAudit({ windowDays = 30, computedAt = new Date() } = {}) {
  if (!pool) throw new Error('Postgres is required for filter verdicts');
  await ensureReady();
  const { rows } = await pool.query(`
    SELECT
      count(*) FILTER (
        WHERE o.scanner='pa'
          AND o.event_ts >= '2026-08-03T14:00:04Z'::timestamptz
          AND o.event_ts <= '2026-08-03T22:39:41Z'::timestamptz
      )::int AS pa_contamination_window,
      count(*) FILTER (WHERE COALESCE(o.scanner,'') IN ('test','test_harness'))::int AS test_scanners,
      count(*) FILTER (WHERE COALESCE(o.ticker,'') LIKE 'ZZ%')::int AS zz_tickers
    FROM signal_outcomes o
    WHERE o.source_type='rejection'
      AND o.event_ts >= $2::timestamptz - ($1 || ' days')::interval
      AND o.event_ts < $2::timestamptz
  `, [windowDays, computedAt]);
  return rows[0];
}

export async function sampleUnderlyingRejections({ scanner, reason, limit = 5 } = {}) {
  if (!pool) throw new Error('Postgres is required for filter verdicts');
  const { rows } = await pool.query(`
    SELECT
      o.source_id,
      o.scanner,
      o.ticker,
      o.direction,
      o.event_ts,
      o.ref_price,
      o.ref_price_source,
      o.ret_1d,
      o.ret_3d,
      COALESCE(NULLIF(r.data->>'reason',''), NULLIF(r.data->>'rejection_reason',''), NULLIF(r.data->>'message',''), 'UNKNOWN') AS reason
    FROM signal_outcomes o
    JOIN rejections r ON r.id::text=o.source_id
    WHERE o.source_type='rejection'
      AND o.scanner=$1
      AND COALESCE(NULLIF(r.data->>'reason',''), NULLIF(r.data->>'rejection_reason',''), NULLIF(r.data->>'message',''), 'UNKNOWN')=$2
      AND o.ret_1d IS NOT NULL
      AND ${analyticsFirewallSql('o','r')}
    ORDER BY o.event_ts DESC
    LIMIT $3
  `, [normalizeScanner(scanner), reason, limit]);
  return rows;
}

function sundaySeventeenUtc(date = new Date()) {
  return date.getUTCDay() === 0 && date.getUTCHours() === 17;
}

export function attachLayer2(app, _db, { adminOnly }) {
  app.post('/api/intelligence/filters/run', adminOnly, async (req, res) => {
    try {
      res.json(await runFilterVerdicts({ windowDays: Number(req.body?.window_days || 30), crossEra: req.body?.cross_era === true }));
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/intelligence/filters', adminOnly, async (req, res) => {
    try {
      res.json(await getLatestFilterVerdicts({ scanner: req.query.scanner }));
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  if (!weeklyStarted && pool && process.env.DISABLE_FILTER_VERDICTS_JOB !== 'true') {
    weeklyStarted = true;
    setInterval(() => {
      const now = new Date();
      const dateKey = now.toISOString().slice(0, 10);
      if (!sundaySeventeenUtc(now) || lastWeeklyRunDate === dateKey) return;
      lastWeeklyRunDate = dateKey;
      runFilterVerdicts({ windowDays: 30 }).catch(e => console.error('[filter-verdicts]', e.message));
    }, 5 * 60_000).unref?.();
  }
}
