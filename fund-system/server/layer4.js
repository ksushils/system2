import pg from 'pg';
import { analyticsFirewallSql } from './analytics-firewall.js';

const { Pool } = pg;
const DATABASE_URL = process.env.DATABASE_URL || '';
const pool = DATABASE_URL ? new Pool({ connectionString: DATABASE_URL, max: Number(process.env.LAYER4_POOL_MAX || 2), application_name: 'fund-system-layer4' }) : null;

let snapshotStarted = false;

function num(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function etParts(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    weekday: 'short',
    hour: '2-digit',
    hour12: false
  }).formatToParts(date);
  return Object.fromEntries(parts.map(p => [p.type, p.value]));
}

function isUsMarketHours(date = new Date()) {
  const parts = etParts(date);
  const hour = Number(parts.hour);
  return !['Sat', 'Sun'].includes(parts.weekday) && hour >= 9 && hour < 16;
}

function isOvernightCryptoSlot(date = new Date()) {
  return date.getUTCHours() === 2 && date.getUTCMinutes() < 30;
}

function spyTrend(spyVs20dPct) {
  const pct = num(spyVs20dPct);
  if (pct === null) return null;
  if (pct > 1) return 'UP';
  if (pct < -1) return 'DOWN';
  return 'FLAT';
}

async function fetchJson(url, timeoutMs = 8000) {
  const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs), headers: { Accept: 'application/json' } });
  const text = await response.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  return { ok: response.ok, status: response.status, body };
}

function withTimeout(promise, timeoutMs, fallback) {
  return Promise.race([
    promise,
    new Promise(resolve => setTimeout(() => resolve(fallback), timeoutMs))
  ]);
}

export async function fetchBtc24hPct() {
  const result = await fetchJson('https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT');
  if (!result.ok) return { ok: false, status: result.status, error: result.body };
  const pct = num(result.body?.priceChangePercent);
  return pct === null ? { ok: false, status: result.status, error: 'missing priceChangePercent' } : { ok: true, status: result.status, btc_24h_pct: pct };
}

async function fetchBinanceCloseAt(ts) {
  const target = ts.getTime();
  const start = target - 60 * 60 * 1000;
  const end = target + 60 * 60 * 1000;
  const url = new URL('https://api.binance.com/api/v3/klines');
  url.searchParams.set('symbol', 'BTCUSDT');
  url.searchParams.set('interval', '1h');
  url.searchParams.set('startTime', String(start));
  url.searchParams.set('endTime', String(end));
  url.searchParams.set('limit', '3');
  const result = await fetchJson(url);
  if (!result.ok || !Array.isArray(result.body)) return null;
  let best = null;
  for (const k of result.body) {
    const openTime = Number(k?.[0]);
    if (!Number.isFinite(openTime)) continue;
    const dist = Math.abs(openTime - target);
    if (!best || dist < best.dist) best = { dist, close: num(k?.[4]) };
  }
  return best?.close || null;
}

export async function fetchHistoricalBtc24hPct(ts) {
  const nowClose = await fetchBinanceCloseAt(ts);
  const priorClose = await fetchBinanceCloseAt(new Date(ts.getTime() - 24 * 60 * 60 * 1000));
  if (!nowClose || !priorClose) return { ok: false, error: 'missing historical BTC candle' };
  return { ok: true, btc_24h_pct: +(((nowClose - priorClose) / priorClose) * 100).toFixed(6) };
}

export async function initLayer4() {
  if (!pool) return false;
  await pool.query(`
    CREATE TABLE IF NOT EXISTS regime_snapshots (
      id BIGSERIAL PRIMARY KEY,
      ts TIMESTAMPTZ NOT NULL DEFAULT now(),
      vix NUMERIC,
      spy_price NUMERIC,
      spy_vs_20d_pct NUMERIC,
      spy_trend TEXT CHECK (spy_trend IN ('UP','DOWN','FLAT') OR spy_trend IS NULL),
      btc_24h_pct NUMERIC,
      dxy NUMERIC,
      session_hour_et INTEGER,
      day_of_week INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_regime_snapshots_ts ON regime_snapshots(ts DESC);
    CREATE TABLE IF NOT EXISTS regime_snapshot_runs (
      id BIGSERIAL PRIMARY KEY,
      started_at TIMESTAMPTZ DEFAULT now(),
      finished_at TIMESTAMPTZ,
      status TEXT NOT NULL,
      wrote_snapshot BOOLEAN DEFAULT false,
      snapshot_id BIGINT,
      fmp_status TEXT,
      binance_status TEXT,
      details JSONB DEFAULT '{}'::jsonb
    );
  `);
  return true;
}

async function recordRun({ started, status, wroteSnapshot, snapshotId, fmpStatus, binanceStatus, details = {} }) {
  if (!pool) return null;
  const { rows } = await pool.query(`
    INSERT INTO regime_snapshot_runs(started_at,finished_at,status,wrote_snapshot,snapshot_id,fmp_status,binance_status,details)
    VALUES($1,now(),$2,$3,$4,$5,$6,$7)
    RETURNING *
  `, [started, status, !!wroteSnapshot, snapshotId || null, fmpStatus || null, binanceStatus || null, details]);
  return rows[0];
}

export async function runRegimeSnapshot({ fetchFmpRegime, ts = new Date(), btcFetcher = fetchBtc24hPct, forceNoData = false } = {}) {
  if (!pool) throw new Error('Postgres is required for regime snapshots');
  await initLayer4();
  const started = new Date();
  const at = ts instanceof Date ? ts : new Date(ts);
  if (!Number.isFinite(at.getTime())) throw new Error('valid ts required');

  const [fmp, btc] = forceNoData
    ? [{ ok: false, status: 'simulated_skip' }, { ok: false, status: 'simulated_skip' }]
    : await Promise.all([
      fetchFmpRegime
        ? withTimeout(
          fetchFmpRegime().catch(e => ({ ok: false, status: 'error', error: e.message })),
          Number(process.env.LAYER4_FMP_TIMEOUT_MS || 3000),
          { ok: false, status: 'timeout', error: 'FMP regime fetch timed out' }
        )
        : Promise.resolve({ ok: false, status: 'unconfigured' }),
      btcFetcher(at).catch(e => ({ ok: false, status: 'error', error: e.message }))
    ]);

  const fmpOk = !!fmp?.ok;
  const btcOk = !!btc?.ok && num(btc.btc_24h_pct) !== null;
  if (!fmpOk && !btcOk) {
    const run = await recordRun({
      started,
      status: 'skipped_no_data',
      wroteSnapshot: false,
      fmpStatus: String(fmp?.status || 'failed'),
      binanceStatus: String(btc?.status || 'failed'),
      details: { fmp_error: fmp?.error || fmp?.body || null, binance_error: btc?.error || null }
    });
    console.log('[regime-snapshot] skipped_no_data', JSON.stringify(run));
    return { status: 'skipped_no_data', run };
  }

  const parts = etParts(at);
  const spyVs20d = fmpOk ? num(fmp.spy_vs_20d_pct) : null;
  const { rows } = await pool.query(`
    INSERT INTO regime_snapshots(ts,vix,spy_price,spy_vs_20d_pct,spy_trend,btc_24h_pct,dxy,session_hour_et,day_of_week)
    VALUES($1,$2,$3,$4,$5,$6,$7,$8,EXTRACT(ISODOW FROM $1::timestamptz)::int)
    RETURNING *
  `, [
    at,
    fmpOk ? num(fmp.vix) : null,
    fmpOk ? num(fmp.spy_price) : null,
    spyVs20d,
    spyTrend(spyVs20d),
    btcOk ? num(btc.btc_24h_pct) : null,
    fmpOk ? num(fmp.dxy) : null,
    Number(parts.hour)
  ]);
  const run = await recordRun({
    started,
    status: fmpOk ? (btcOk ? 'full' : 'fmp_only') : 'btc_only',
    wroteSnapshot: true,
    snapshotId: rows[0].id,
    fmpStatus: String(fmp?.status || (fmpOk ? 'ok' : 'failed')),
    binanceStatus: String(btc?.status || (btcOk ? 'ok' : 'failed')),
    details: { fmp_cache: fmp?.cache || null }
  });
  return { status: run.status, snapshot: rows[0], run };
}

export async function runRegimeSnapshotBackfillHours({ fetchFmpRegime, hours = [] } = {}) {
  const output = [];
  for (const h of hours) {
    const offset = Math.max(0, Number(h) || 0);
    const ts = new Date(Date.now() - offset * 60 * 60 * 1000);
    output.push(await runRegimeSnapshot({ fetchFmpRegime, ts, btcFetcher: fetchHistoricalBtc24hPct }));
  }
  return output;
}

export async function latestRegimeSnapshots(limit = 10) {
  if (!pool) throw new Error('Postgres is required for regime snapshots');
  await initLayer4();
  const { rows } = await pool.query(`
    SELECT id, ts, vix::float8, spy_price::float8, spy_vs_20d_pct::float8, spy_trend,
      btc_24h_pct::float8, dxy::float8, session_hour_et, day_of_week
    FROM regime_snapshots
    ORDER BY ts DESC
    LIMIT $1
  `, [Math.max(1, Math.min(100, Number(limit) || 10))]);
  return rows;
}

export async function latestRegimeRuns(limit = 10) {
  if (!pool) throw new Error('Postgres is required for regime snapshot runs');
  await initLayer4();
  const { rows } = await pool.query(`
    SELECT id, started_at, finished_at, status, wrote_snapshot, snapshot_id, fmp_status, binance_status, details
    FROM regime_snapshot_runs
    ORDER BY id DESC
    LIMIT $1
  `, [Math.max(1, Math.min(100, Number(limit) || 10))]);
  return rows;
}

export async function regimePerformance(scanner = '') {
  if (!pool) throw new Error('Postgres is required for regime performance');
  await initLayer4();
  const params = [];
  const filters = [`o.ret_1d IS NOT NULL`,analyticsFirewallSql('o')];
  if (scanner) {
    params.push(scanner);
    filters.push(`o.scanner=$${params.length}`);
  }
  const { rows } = await pool.query(`
    WITH joined AS (
      SELECT o.id outcome_id, o.scanner, o.event_ts, o.ret_1d::float8, rs.id snapshot_id,
        rs.ts snapshot_ts, rs.spy_trend, rs.vix::float8 vix, rs.session_hour_et,
        abs(extract(epoch from (rs.ts - o.event_ts))) AS distance_seconds
      FROM signal_outcomes o
      JOIN LATERAL (
        SELECT *
        FROM regime_snapshots rs
        WHERE rs.ts BETWEEN o.event_ts - interval '45 minutes' AND o.event_ts + interval '45 minutes'
        ORDER BY abs(extract(epoch from (rs.ts - o.event_ts))) ASC, rs.id ASC
        LIMIT 1
      ) rs ON true
      WHERE ${filters.join(' AND ')}
    ),
    terciles AS (
      SELECT percentile_cont(0.333) WITHIN GROUP (ORDER BY vix) AS p33,
             percentile_cont(0.667) WITHIN GROUP (ORDER BY vix) AS p67
      FROM joined
      WHERE vix IS NOT NULL
    )
    SELECT COALESCE(j.spy_trend,'BTC_ONLY') spy_trend,
      CASE
        WHEN j.vix IS NULL THEN 'UNKNOWN'
        WHEN j.vix <= t.p33 THEN 'LOW'
        WHEN j.vix <= t.p67 THEN 'MID'
        ELSE 'HIGH'
      END AS vix_tercile,
      j.session_hour_et,
      count(*)::int n,
      round(avg(j.ret_1d)::numeric, 6)::float8 avg_ret_1d,
      (count(*) < 30) insufficient
    FROM joined j CROSS JOIN terciles t
    GROUP BY COALESCE(j.spy_trend,'BTC_ONLY'), vix_tercile, j.session_hour_et
    ORDER BY spy_trend, vix_tercile, session_hour_et
  `, params);
  return { scanner: scanner || 'all', rows, note: 'signal_outcomes without a regime_snapshot within 45 minutes are excluded.' };
}

export async function nearestJoinProof(scanner = '') {
  if (!pool) throw new Error('Postgres is required for regime performance');
  await initLayer4();
  const params = [];
  let scannerFilter = '';
  if (scanner) {
    params.push(scanner);
    scannerFilter = `AND o.scanner=$${params.length}`;
  }
  const { rows } = await pool.query(`
    SELECT o.id outcome_id, o.scanner, o.event_ts, chosen.snapshot_id, chosen.snapshot_ts,
      round(chosen.distance_seconds::numeric, 3)::float8 chosen_distance_seconds,
      candidates.candidate_count::int,
      candidates.min_distance_seconds::float8
    FROM signal_outcomes o
    JOIN LATERAL (
      SELECT rs.id snapshot_id, rs.ts snapshot_ts,
        abs(extract(epoch from (rs.ts - o.event_ts))) AS distance_seconds
      FROM regime_snapshots rs
      WHERE rs.ts BETWEEN o.event_ts - interval '45 minutes' AND o.event_ts + interval '45 minutes'
      ORDER BY abs(extract(epoch from (rs.ts - o.event_ts))) ASC, rs.id ASC
      LIMIT 1
    ) chosen ON true
    JOIN LATERAL (
      SELECT count(*) candidate_count, min(abs(extract(epoch from (rs.ts - o.event_ts)))) min_distance_seconds
      FROM regime_snapshots rs
      WHERE rs.ts BETWEEN o.event_ts - interval '45 minutes' AND o.event_ts + interval '45 minutes'
    ) candidates ON true
    WHERE o.ret_1d IS NOT NULL AND ${analyticsFirewallSql('o')} ${scannerFilter}
    ORDER BY candidates.candidate_count DESC, o.event_ts DESC
    LIMIT 5
  `, params);
  return rows;
}

export function attachLayer4(app, _db, { adminOnly, fetchFmpRegime }) {
  app.post('/api/intelligence/regime-snapshot/run', adminOnly, async (req, res) => {
    try {
      if (Array.isArray(req.body?.backfill_hours)) {
        return res.json({ rows: await runRegimeSnapshotBackfillHours({ fetchFmpRegime, hours: req.body.backfill_hours }) });
      }
      if (req.body?.ts) {
        return res.json(await runRegimeSnapshot({
          fetchFmpRegime,
          ts: new Date(req.body.ts),
          btcFetcher: fetchHistoricalBtc24hPct,
          forceNoData: req.body?.force_no_data === true
        }));
      }
      res.json(await runRegimeSnapshot({ fetchFmpRegime, forceNoData: req.body?.force_no_data === true }));
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/intelligence/regime-snapshots', adminOnly, async (req, res) => {
    try { res.json({ rows: await latestRegimeSnapshots(req.query.limit) }); }
    catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.get('/api/intelligence/regime-runs', adminOnly, async (req, res) => {
    try { res.json({ rows: await latestRegimeRuns(req.query.limit) }); }
    catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.get('/api/intelligence/regime-performance', adminOnly, async (req, res) => {
    try { res.json(await regimePerformance(String(req.query.scanner || '').toLowerCase())); }
    catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.get('/api/intelligence/regime-nearest-proof', adminOnly, async (req, res) => {
    try { res.json({ rows: await nearestJoinProof(String(req.query.scanner || '').toLowerCase()) }); }
    catch (e) { res.status(500).json({ error: e.message }); }
  });
}

export function startLayer4Jobs({ fetchFmpRegime }) {
  if (!pool || snapshotStarted || process.env.DISABLE_LAYER4_JOBS === 'true') return;
  snapshotStarted = true;
  setInterval(() => {
    const now = new Date();
    if ((now.getUTCMinutes() === 0 || now.getUTCMinutes() === 30) && (isUsMarketHours(now) || isOvernightCryptoSlot(now))) {
      runRegimeSnapshot({ fetchFmpRegime }).catch(e => console.error('[regime-snapshot]', e.message));
    }
  }, 60_000).unref?.();
}
