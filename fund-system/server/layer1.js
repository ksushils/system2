import pg from 'pg';

const { Pool } = pg;
const DATABASE_URL = process.env.DATABASE_URL || '';
const pool = DATABASE_URL ? new Pool({ connectionString: DATABASE_URL, max: Number(process.env.LAYER1_POOL_MAX || 2), application_name: 'fund-system-layer1' }) : null;

const SCANNERS_STOCK = new Set(['fmp', 'fmp_alpaca', 'pa', 'failed_breakout', 'volume', 'mean_reversion']);
const SCANNERS_CRYPTO = new Set(['crypto']);
const VALID_SOURCES = new Set(['signal', 'rejection']);
let lastRun = null;
let hourlyStarted = false;
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

function num(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function cleanTicker(v) {
  return String(v || '').trim().toUpperCase();
}

function normalizeDirection(v) {
  const d = String(v || '').trim().toUpperCase();
  if (['SELL', 'SHORT'].includes(d)) return 'SHORT';
  if (['BUY', 'LONG'].includes(d)) return 'LONG';
  return null;
}

function eventTsFor(sourceType, data) {
  const raw = sourceType === 'rejection'
    ? (data?.rejected_time || data?.ts)
    : data?.ts;
  const d = raw ? new Date(raw) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
}

function pctRet(ref, future, direction) {
  ref = num(ref); future = num(future);
  if (!ref || !future) return null;
  const raw = direction === 'SHORT' ? (ref - future) / ref : (future - ref) / ref;
  return +raw.toFixed(6);
}

function priceFromCandle(c) {
  return num(c?.close ?? c?.price ?? c?.closePrice?.bid ?? c?.closePrice?.ask ?? c?.closePrice);
}

function candleTime(c) {
  const raw = c?.date || c?.time || c?.openTime || c?.snapshotTimeUTC || c?.snapshotTime || c?.timestamp;
  const d = raw ? new Date(typeof raw === 'number' ? raw : raw.replace?.(' ', 'T') || raw) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
}

function sortCandles(candles) {
  return (Array.isArray(candles) ? candles : [])
    .map(c => ({ ...c, _t: candleTime(c) }))
    .filter(c => c._t)
    .sort((a, b) => a._t - b._t);
}

function nearestClose(candles, target) {
  const t = target.getTime();
  let best = null;
  for (const c of candles) {
    if (c._t.getTime() <= t) best = c;
    if (c._t.getTime() >= t) return priceFromCandle(c);
  }
  return best ? priceFromCandle(best) : null;
}

async function fetchFmpCandles(ticker, start, end, fmpProxyFetch) {
  if (!fmpProxyFetch) return { candles: [], rateLimited: false };
  const from = start.toISOString().slice(0, 10);
  const to = end.toISOString().slice(0, 10);
  const endpoint = `api/v3/historical-chart/1hour/${encodeURIComponent(ticker)}?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
  const result = await fmpProxyFetch(`l1:h1:${ticker}:${from}:${to}`, 24 * 60 * 60 * 1000, endpoint);
  if (result?.status === 429) return { candles: [], rateLimited: true };
  return { candles: sortCandles(result?.data || result || []), rateLimited: false };
}

async function fetchBinanceCandles(ticker, start, end) {
  let symbol = cleanTicker(ticker).replace(/[-/]/g, '');
  if (!symbol.endsWith('USDT')) symbol += 'USDT';
  const url = new URL('https://api.binance.com/api/v3/klines');
  url.searchParams.set('symbol', symbol);
  url.searchParams.set('interval', '1h');
  url.searchParams.set('startTime', String(start.getTime()));
  url.searchParams.set('endTime', String(end.getTime()));
  url.searchParams.set('limit', '1000');
  const r = await fetch(url, { signal: AbortSignal.timeout(8000) });
  if (!r.ok) return { candles: [], rateLimited: r.status === 429 };
  const rows = await r.json();
  return {
    candles: sortCandles(rows.map(k => ({
      openTime: k[0], open: k[1], high: k[2], low: k[3], close: k[4]
    }))),
    rateLimited: false
  };
}

async function fetchCapitalCandles() {
  return { candles: [], rateLimited: false, unsupported: true };
}

async function fetchCandles(scanner, ticker, eventTs, fmpProxyFetch) {
  const start = new Date(eventTs.getTime() - 60 * 60 * 1000);
  const end = new Date(eventTs.getTime() + 3 * 24 * 60 * 60 * 1000 + 2 * 60 * 60 * 1000);
  if (SCANNERS_CRYPTO.has(scanner)) return fetchBinanceCandles(ticker, start, end);
  if (SCANNERS_STOCK.has(scanner)) return fetchFmpCandles(ticker, start, end, fmpProxyFetch);
  return fetchCapitalCandles(scanner, ticker, start, end);
}

function barrier(candles, eventTs, direction, tp, sl) {
  tp = num(tp); sl = num(sl);
  if (!tp || !sl) return null;
  const forwardCandles = candles.filter(c => c._t >= eventTs);
  if (!forwardCandles.length) return null;
  for (const c of forwardCandles) {
    const high = num(c.high ?? c.highPrice?.bid ?? c.highPrice?.ask);
    const low = num(c.low ?? c.lowPrice?.bid ?? c.lowPrice?.ask);
    if (high == null || low == null) continue;
    const tpHit = direction === 'SHORT' ? low <= tp : high >= tp;
    const slHit = direction === 'SHORT' ? high >= sl : low <= sl;
    if (tpHit && slHit) return 'SL_FIRST';
    if (tpHit) return 'TP_FIRST';
    if (slHit) return 'SL_FIRST';
  }
  return 'NEITHER';
}

export async function initLayer1() {
  if (!pool) return;
  await pool.query(`
    CREATE TABLE IF NOT EXISTS signal_outcomes (
      id BIGSERIAL PRIMARY KEY,
      source_type TEXT NOT NULL CHECK (source_type IN ('signal','rejection')),
      source_id TEXT NOT NULL,
      scanner TEXT,
      ticker TEXT,
      direction TEXT,
      direction_assumed BOOLEAN DEFAULT false,
      ref_price NUMERIC,
      ref_price_source TEXT CHECK (ref_price_source IN ('payload','fetched_candle')),
      event_ts TIMESTAMPTZ,
      ret_1h NUMERIC,
      ret_1d NUMERIC,
      ret_3d NUMERIC,
      tp_price NUMERIC,
      sl_price NUMERIC,
      barrier_outcome TEXT CHECK (barrier_outcome IN ('TP_FIRST','SL_FIRST','NEITHER') OR barrier_outcome IS NULL),
      labeled_at TIMESTAMPTZ DEFAULT now(),
      UNIQUE(source_type, source_id)
    );
    CREATE INDEX IF NOT EXISTS idx_signal_outcomes_scanner_ts ON signal_outcomes(scanner, event_ts);
    CREATE TABLE IF NOT EXISTS outcome_label_runs (
      id BIGSERIAL PRIMARY KEY,
      started_at TIMESTAMPTZ DEFAULT now(),
      finished_at TIMESTAMPTZ,
      labeled INTEGER DEFAULT 0,
      skipped_no_price INTEGER DEFAULT 0,
      rate_limited INTEGER DEFAULT 0,
      details JSONB DEFAULT '{}'::jsonb
    );
    CREATE TABLE IF NOT EXISTS outcome_label_skips (
      source_type TEXT NOT NULL CHECK (source_type IN ('signal','rejection')),
      source_id TEXT NOT NULL,
      scanner TEXT,
      ticker TEXT,
      event_ts TIMESTAMPTZ,
      reason TEXT NOT NULL,
      attempts INTEGER DEFAULT 1,
      last_attempt_at TIMESTAMPTZ DEFAULT now(),
      PRIMARY KEY(source_type, source_id)
    );
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS max_favorable NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS max_adverse NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS mae_r NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS mfe_r NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS intended_entry NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS initial_sl NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS fill_slippage_pct NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS excluded_from_expectancy BOOLEAN DEFAULT false;
  `);
}

async function nextUnlabeled(limit) {
  const { rows } = await pool.query(`
    WITH src AS (
      SELECT 'signal'::text source_type, id::text source_id, scanner, ticker, data,
        NULLIF(data->>'ts','')::timestamptz event_ts
      FROM signals
      UNION ALL
      SELECT 'rejection'::text source_type, id::text source_id, scanner, ticker, data,
        COALESCE(NULLIF(data->>'rejected_time',''), NULLIF(data->>'ts',''))::timestamptz event_ts
      FROM rejections
    )
    SELECT * FROM src
    WHERE event_ts IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM signal_outcomes o
        WHERE o.source_type=src.source_type AND o.source_id=src.source_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM outcome_label_skips s
        WHERE s.source_type=src.source_type AND s.source_id=src.source_id
      )
    ORDER BY event_ts ASC
    LIMIT $1
  `, [limit]);
  return rows;
}

async function recordSkip(row, reason) {
  await pool.query(`
    INSERT INTO outcome_label_skips(source_type,source_id,scanner,ticker,event_ts,reason,attempts,last_attempt_at)
    VALUES($1,$2,$3,$4,$5,$6,1,now())
    ON CONFLICT(source_type,source_id) DO UPDATE
      SET attempts=outcome_label_skips.attempts+1,
          reason=$6,
          last_attempt_at=now()
  `, [row.source_type, row.source_id, row.scanner, row.ticker, row.event_ts, reason]);
}

async function labelRow(row, fmpProxyFetch) {
  const data = row.data || {};
  const scanner = String(row.scanner || data.scanner || 'unknown').toLowerCase();
  const ticker = cleanTicker(row.ticker || data.ticker || data.symbol || data.epic);
  const eventTs = eventTsFor(row.source_type, data);
  if (!eventTs || !ticker) return { skipped: true, reason: 'missing event_ts_or_ticker' };
  let direction = normalizeDirection(data.direction || data.type || data.side || data.action);
  const directionAssumed = !direction;
  if (!direction) direction = 'LONG';

  let refPrice = null;
  let refPriceSource = 'payload';
  if (row.source_type === 'signal') {
    refPrice = num(data.entry ?? data.entry_price ?? data.intended_entry);
  } else {
    refPrice = num(data.rejected_price);
    if (!refPrice) refPriceSource = 'fetched_candle';
  }

  const pricePack = await fetchCandles(scanner, ticker, eventTs, fmpProxyFetch);
  if (pricePack.rateLimited) return { rateLimited: true };
  const candles = pricePack.candles || [];
  if (!refPrice && refPriceSource === 'fetched_candle') refPrice = nearestClose(candles, eventTs);
  if (!refPrice) return { skipped: true, reason: pricePack.unsupported ? 'price_provider_unavailable' : 'no_ref_price' };

  const p1h = nearestClose(candles, new Date(eventTs.getTime() + 60 * 60 * 1000));
  const p1d = nearestClose(candles, new Date(eventTs.getTime() + 24 * 60 * 60 * 1000));
  const p3d = nearestClose(candles, new Date(eventTs.getTime() + 3 * 24 * 60 * 60 * 1000));
  const tp = num(data.tp ?? data.tp1 ?? data.take_profit_1 ?? data.intended_tp1);
  const sl = num(data.sl ?? data.stop_loss ?? data.intended_sl);
  const barrierOutcome = (tp && sl) ? barrier(candles, eventTs, direction, tp, sl) : null;

  await pool.query(`
    INSERT INTO signal_outcomes(source_type,source_id,scanner,ticker,direction,direction_assumed,ref_price,ref_price_source,event_ts,ret_1h,ret_1d,ret_3d,tp_price,sl_price,barrier_outcome,labeled_at)
    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,now())
    ON CONFLICT(source_type,source_id) DO NOTHING
  `, [
    row.source_type, row.source_id, scanner, ticker, direction, directionAssumed, refPrice,
    refPriceSource, eventTs, pctRet(refPrice, p1h, direction), pctRet(refPrice, p1d, direction),
    pctRet(refPrice, p3d, direction), tp, sl, row.source_type === 'rejection' && !(tp && sl) ? null : barrierOutcome
  ]);
  return { labeled: true };
}

export async function runOutcomeBackfill({ limit = 200, fmpProxyFetch } = {}) {
  if (!pool) throw new Error('Postgres is required for outcome labeling');
  const started = new Date();
  const run = { labeled: 0, skipped_no_price: 0, rate_limited: 0, details: {}, started_at: started.toISOString() };
  const rows = await nextUnlabeled(limit);
  for (let idx = 0; idx < rows.length; idx += 1) {
    const row = rows[idx];
    if (idx > 0 && idx % Number(process.env.LAYER1_BATCH_YIELD_EVERY || 20) === 0) {
      await sleep(Number(process.env.LAYER1_BATCH_YIELD_MS || 250));
    }
    try {
      const result = await labelRow(row, fmpProxyFetch);
      if (result.labeled) run.labeled += 1;
      else if (result.rateLimited) run.rate_limited += 1;
      else {
        run.skipped_no_price += 1;
        const reason = result.reason || 'skipped';
        await recordSkip(row, reason);
        run.details[reason] = (run.details[reason] || 0) + 1;
      }
    } catch (err) {
      run.skipped_no_price += 1;
      const key = String(err.message || 'error').slice(0, 120);
      await recordSkip(row, key);
      run.details[key] = (run.details[key] || 0) + 1;
    }
  }
  const { rows: inserted } = await pool.query(
    `INSERT INTO outcome_label_runs(started_at,finished_at,labeled,skipped_no_price,rate_limited,details)
     VALUES($1,now(),$2,$3,$4,$5) RETURNING *`,
    [started, run.labeled, run.skipped_no_price, run.rate_limited, run.details]
  );
  lastRun = inserted[0];
  console.log('[label-outcomes]', JSON.stringify(lastRun));
  return lastRun;
}

function asAdminQuery(req) {
  return [String(req.query.scanner || '').toLowerCase(), Math.max(1, Number(req.query.days || 30) || 30)];
}

export function attachLayer1(app, db, { adminOnly, fmpProxyFetch }) {
  app.post('/api/intelligence/outcomes/backfill', adminOnly, async (req, res) => {
    try { res.json(await runOutcomeBackfill({ limit: Number(req.body?.limit || 200), fmpProxyFetch })); }
    catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.get('/api/intelligence/outcomes', adminOnly, async (req, res) => {
    try {
      const [scanner, days] = asAdminQuery(req);
      const params = [days];
      let filter = `event_ts >= now() - ($1 || ' days')::interval`;
      if (scanner) { params.push(scanner); filter += ` AND scanner=$2`; }
      const { rows } = await pool.query(`
        SELECT source_type, count(*)::int n, round(avg(ret_1d)::numeric,6) avg_ret_1d
        FROM signal_outcomes WHERE ${filter} GROUP BY source_type ORDER BY source_type
      `, params);
      res.json({ scanner: scanner || 'all', days, summary: rows, last_run: lastRun });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.get('/api/intelligence/excursions', adminOnly, async (req, res) => {
    try {
      const scanner = String(req.query.scanner || '').toLowerCase();
      const setup = String(req.query.setup || '');
      const params = [];
      const where = [`mae_r IS NOT NULL`, `mfe_r IS NOT NULL`];
      if (scanner) { params.push(scanner); where.push(`scanner=$${params.length}`); }
      if (setup) { params.push(setup); where.push(`setup_type=$${params.length}`); }
      const { rows } = await pool.query(`
        SELECT scanner, COALESCE(NULLIF(setup_type,''),'UNKNOWN') setup_type, count(*)::int n,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY mae_r) FILTER (WHERE pnl > 0) winners_median_mae,
          percentile_cont(0.9) WITHIN GROUP (ORDER BY abs(mae_r)) FILTER (WHERE pnl > 0) winners_p90_mae,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY mfe_r) FILTER (WHERE pnl > 0) winners_median_mfe,
          round(avg(CASE WHEN mfe_r > 0 AND close_price IS NOT NULL AND entry IS NOT NULL
            THEN abs(close_price-entry) / NULLIF(abs(max_favorable-entry),0) END)::numeric,4) exit_efficiency
        FROM trades WHERE ${where.join(' AND ')}
        GROUP BY scanner, COALESCE(NULLIF(setup_type,''),'UNKNOWN')
        HAVING count(*) >= 10 ORDER BY scanner, setup_type
      `, params);
      res.json({ scanner: scanner || 'all', setup: setup || 'all', rows: rows.map(r => ({ ...r, insufficient: r.n < 30 })) });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.get('/api/intelligence/slippage', adminOnly, async (req, res) => {
    try {
      const scanner = String(req.query.scanner || '').toLowerCase();
      const params = [];
      let filter = `fill_slippage_pct IS NOT NULL`;
      if (scanner) { params.push(scanner); filter += ` AND scanner=$1`; }
      const { rows } = await pool.query(`
        SELECT scanner, extract(hour from opened_at)::int hour_of_day, count(*)::int n,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY fill_slippage_pct) median,
          percentile_cont(0.9) WITHIN GROUP (ORDER BY fill_slippage_pct) p90,
          max(fill_slippage_pct) worst
        FROM trades WHERE ${filter}
        GROUP BY scanner, extract(hour from opened_at)
        ORDER BY scanner, hour_of_day
      `, params);
      res.json({ scanner: scanner || 'all', rows });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.post('/api/intelligence/recover-archived', adminOnly, async (_req, res) => {
    try {
      const archived = (db.data.trades || []).filter(t => t.status === 'ARCHIVED' && (t.pnl === null || t.pnl === undefined));
      for (const t of archived) t.excluded_from_expectancy = true;
      if (pool) {
        await pool.query(`
          UPDATE trades
          SET excluded_from_expectancy=true,
              data=jsonb_set(COALESCE(data,'{}'::jsonb), '{excluded_from_expectancy}', 'true'::jsonb, true)
          WHERE status='ARCHIVED' AND pnl IS NULL
        `);
      }
      if (archived.length) await db.write();
      res.json({ archived_without_pnl: archived.length, matched_from_capital: 0, excluded_from_expectancy: archived.length, note: 'Capital transaction matching unavailable from dashboard-held data; archived no-PnL trades excluded from expectancy.' });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  if (!hourlyStarted && pool && process.env.DISABLE_OUTCOME_BACKFILL !== 'true') {
    hourlyStarted = true;
    setInterval(() => runOutcomeBackfill({ limit: 200, fmpProxyFetch }).catch(e => console.error('[label-outcomes]', e.message)), 60 * 60 * 1000).unref?.();
  }
}

export function computeFillSlippage(direction, entry, intendedEntry) {
  entry = num(entry); intendedEntry = num(intendedEntry);
  if (!entry || !intendedEntry) return null;
  const d = normalizeDirection(direction) || 'LONG';
  const raw = d === 'SHORT' ? (intendedEntry - entry) / intendedEntry : (entry - intendedEntry) / intendedEntry;
  return +(raw * 100).toFixed(6);
}

export function applyTradePricePath(t, currentPrice) {
  const p = num(currentPrice);
  const entry = num(t?.entry ?? t?.entry_price);
  if (!t || !p || !entry) return false;
  const d = normalizeDirection(t.direction) || 'LONG';
  if (t.max_favorable == null) t.max_favorable = entry;
  if (t.max_adverse == null) t.max_adverse = entry;
  if (d === 'SHORT') {
    t.max_favorable = Math.min(num(t.max_favorable) ?? entry, p);
    t.max_adverse = Math.max(num(t.max_adverse) ?? entry, p);
  } else {
    t.max_favorable = Math.max(num(t.max_favorable) ?? entry, p);
    t.max_adverse = Math.min(num(t.max_adverse) ?? entry, p);
  }
  return true;
}

export function finalizeTradePath(t) {
  const entry = num(t?.entry ?? t?.entry_price);
  const sl = num(t?.initial_sl ?? t?.open_sl ?? t?.sl ?? t?.stop_loss);
  if (!t || !entry || !sl) return;
  const risk = Math.abs(entry - sl);
  if (!risk) return;
  const d = normalizeDirection(t.direction) || 'LONG';
  const fav = num(t.max_favorable) ?? entry;
  const adv = num(t.max_adverse) ?? entry;
  t.mfe_r = +(d === 'SHORT' ? (entry - fav) / risk : (fav - entry) / risk).toFixed(4);
  t.mae_r = +(d === 'SHORT' ? (entry - adv) / risk : (adv - entry) / risk).toFixed(4);
}
