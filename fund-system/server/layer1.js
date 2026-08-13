import pg from 'pg';
import { analyticsFirewallSql } from './analytics-firewall.js';

const { Pool } = pg;
const DATABASE_URL = process.env.DATABASE_URL || '';
const pool = DATABASE_URL ? new Pool({ connectionString: DATABASE_URL, max: Number(process.env.LAYER1_POOL_MAX || 2), application_name: 'fund-system-layer1' }) : null;

const SCANNERS_STOCK = new Set(['fmp', 'fmp_alpaca', 'pa', 'failed_breakout', 'volume', 'mean_reversion']);
const SCANNERS_CRYPTO = new Set(['crypto']);
const VALID_SOURCES = new Set(['signal', 'rejection']);
let lastRun = null;
let hourlyStarted = false;
let capitalSession = null;
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const MAX_SKIP_ATTEMPTS = 5;

function num(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function cleanTicker(v) {
  return String(v || '').trim().toUpperCase();
}

export function skipRetryAfter(reason, eventTs, now = new Date()) {
  const normalized = String(reason || '').trim().toLowerCase();
  if (normalized === 'price_provider_unavailable') {
    const event = eventTs ? new Date(eventTs) : null;
    return event && !Number.isNaN(event.getTime())
      ? new Date(event.getTime() + 4 * 24 * 60 * 60 * 1000)
      : new Date(now.getTime() + 60 * 60 * 1000);
  }
  if (normalized === 'no_ref_price') {
    return new Date(now.getTime() + 60 * 60 * 1000);
  }
  if (normalized === 'bad_tick_suspected') return new Date(now.getTime() + 24 * 60 * 60 * 1000);
  if (/(rate.?limit|\b429\b|timeout|timed out|abort)/i.test(normalized)) {
    return new Date(now.getTime() + 60 * 60 * 1000);
  }
  return null;
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
  const endpoint = `stable/historical-chart/1hour?symbol=${encodeURIComponent(ticker)}&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
  const result = await fmpProxyFetch(`l1:h1:${ticker}:${from}:${to}`, 24 * 60 * 60 * 1000, endpoint);
  if (result?.status === 429) {
    const deferred = ['BUDGET_BLOCKED', 'PAUSED'].includes(result?.cache);
    let retryAfter = result?.body?.paused_until ? new Date(result.body.paused_until) : null;
    if (!retryAfter || Number.isNaN(retryAfter.getTime())) {
      retryAfter = new Date();
      if (result?.cache === 'BUDGET_BLOCKED') {
        retryAfter.setUTCDate(retryAfter.getUTCDate() + 1);
        retryAfter.setUTCHours(0, 5, 0, 0);
      } else {
        retryAfter = new Date(retryAfter.getTime() + 60 * 60 * 1000);
      }
    }
    const rateLimitKind = result?.cache === 'BUDGET_BLOCKED' ? 'budget_blocked'
      : result?.cache === 'PAUSED' ? 'paused'
      : 'upstream_rate_limited';
    return { candles: [], rateLimited: true, rateLimitKind, deferWithoutAttempt: deferred, retryAfter };
  }
  if (Number(result?.status || 200) >= 400) {
    return { candles: [], rateLimited: false, unsupported: true, reason: `fmp_${result.status}` };
  }
  const payload = result?.body ?? result?.data ?? result ?? [];
  const candles = sortCandles(Array.isArray(payload) ? payload : (payload?.historical || []));
  return { candles, rateLimited: false, structural: candles.length === 0 };
}

export function compareSourcePrices(primary, secondary, thresholdPct = 0.5) {
  primary=num(primary); secondary=num(secondary);
  if (!primary || !secondary) return { validated:false, divergence_pct:null, bad_tick:false };
  const divergencePct=Math.abs(primary-secondary)/((primary+secondary)/2)*100;
  return { validated:true, divergence_pct:+divergencePct.toFixed(6), bad_tick:divergencePct>thresholdPct };
}

async function fetchYahooCandles(ticker, start, end) {
  const symbol = encodeURIComponent(cleanTicker(ticker));
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}?period1=${Math.floor(start.getTime()/1000)}&period2=${Math.ceil(end.getTime()/1000)}&interval=1h&events=history`;
  const response = await fetch(url, { headers: { 'User-Agent': 'fund-system-price-validator/1.0' }, signal: AbortSignal.timeout(10000) });
  if (!response.ok) return { candles: [], unavailable: true, status: response.status };
  const body = await response.json();
  const result = body?.chart?.result?.[0];
  const quote = result?.indicators?.quote?.[0] || {};
  return { candles: sortCandles((result?.timestamp || []).map((ts, i) => ({
    timestamp: ts * 1000, open: quote.open?.[i], high: quote.high?.[i], low: quote.low?.[i], close: quote.close?.[i]
  })).filter(c => num(c.close) != null)), unavailable: false };
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
  if (!r.ok) return { candles: [], rateLimited: r.status === 429,
    rateLimitKind: r.status === 429 ? 'upstream_rate_limited' : undefined };
  const rows = await r.json();
  return {
    candles: sortCandles(rows.map(k => ({
      openTime: k[0], open: k[1], high: k[2], low: k[3], close: k[4]
    }))),
    rateLimited: false
  };
}

function envValue(...keys) {
  for (const key of keys) {
    if (process.env[key]) return process.env[key];
  }
  return '';
}

// Exported so the live-position poller shares this ONE cached session
// rather than opening a second login path. The cache is the point: the
// poller was logging in 1,440x/day without one.
export async function capitalLogin() {
  const apiKey = envValue('CAP_API_KEY');
  const identifier = envValue('CAP_IDENTIFIER', 'CAP_EMAIL');
  const password = envValue('CAP_PASSWORD');
  const baseUrl = envValue('CAP_BASE_URL') || 'https://demo-api-capital.backend-capital.com';
  if (!apiKey || !identifier || !password) return { ok: false, error: 'capital_credentials_missing' };
  if (capitalSession?.cst && capitalSession?.token && Date.now() < capitalSession.expires) {
    return { ok: true, apiKey, baseUrl, cst: capitalSession.cst, token: capitalSession.token };
  }
  const response = await fetch(`${baseUrl}/api/v1/session`, {
    method: 'POST',
    headers: { 'X-CAP-API-KEY': apiKey, 'Content-Type': 'application/json' },
    body: JSON.stringify({ identifier, password, encryptedPassword: false }),
    signal: AbortSignal.timeout(8000)
  });
  const text = await response.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!response.ok) return { ok: false, status: response.status, error: body?.errorCode || body?.error || 'capital_login_failed' };
  const cst = response.headers.get('cst') || response.headers.get('CST');
  const token = response.headers.get('x-security-token') || response.headers.get('X-SECURITY-TOKEN');
  if (!cst || !token) return { ok: false, status: response.status, error: 'capital_session_headers_missing' };
  capitalSession = { cst, token, expires: Date.now() + 8 * 60 * 1000 };
  return { ok: true, apiKey, baseUrl, cst, token };
}

// A cached session that has expired early must be droppable by its
// users, or a stale CST becomes an unrecoverable 401 loop.
export function invalidateCapitalSession() {
  capitalSession = null;
}

async function fetchCapitalCandles(scanner, ticker, start, end) {
  const session = await capitalLogin();
  if (!session.ok) return { candles: [], rateLimited: false, unsupported: true, reason: session.error };
  const epic = encodeURIComponent(cleanTicker(ticker));
  const url = new URL(`${session.baseUrl}/api/v1/prices/${epic}`);
  url.searchParams.set('resolution', 'HOUR');
  const capTime = d => d.toISOString().slice(0, 19);
  url.searchParams.set('from', capTime(start));
  url.searchParams.set('to', capTime(end));
  url.searchParams.set('max', '1000');
  const response = await fetch(url, {
    headers: {
      'X-CAP-API-KEY': session.apiKey,
      CST: session.cst,
      'X-SECURITY-TOKEN': session.token,
      Accept: 'application/json'
    },
    signal: AbortSignal.timeout(10000)
  });
  const text = await response.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (response.status === 401) capitalSession = null;
  if (response.status === 429) return { candles: [], rateLimited: true, rateLimitKind: 'upstream_rate_limited' };
  if (!response.ok || body?.errorCode) return { candles: [], rateLimited: false, unsupported: true, reason: body?.errorCode || body?.error || `capital_${response.status}` };
  return { candles: sortCandles(body?.prices || []), rateLimited: false };
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
      measurement_population TEXT CHECK (measurement_population IN ('ENTERED','NEVER_ENTERED')),
      config_hash TEXT,
      price_validated BOOLEAN DEFAULT false,
      secondary_source TEXT,
      primary_validation_price NUMERIC,
      secondary_validation_price NUMERIC,
      price_divergence_pct NUMERIC,
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
      attempt_count INTEGER NOT NULL DEFAULT 1,
      retry_after TIMESTAMPTZ,
      last_attempt_at TIMESTAMPTZ DEFAULT now(),
      details JSONB DEFAULT '{}'::jsonb,
      PRIMARY KEY(source_type, source_id)
    );
    ALTER TABLE outcome_label_skips ADD COLUMN IF NOT EXISTS attempt_count INTEGER;
    ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS measurement_population TEXT;
    ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS config_hash TEXT;
    ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS price_validated BOOLEAN DEFAULT false;
    ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS secondary_source TEXT;
    ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS primary_validation_price NUMERIC;
    ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS secondary_validation_price NUMERIC;
    ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS price_divergence_pct NUMERIC;
    ALTER TABLE outcome_label_skips ADD COLUMN IF NOT EXISTS details JSONB DEFAULT '{}'::jsonb;
    ALTER TABLE outcome_label_skips ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ;
    UPDATE outcome_label_skips
      SET attempt_count=COALESCE(attempt_count,attempts,1)
      WHERE attempt_count IS NULL;
    ALTER TABLE outcome_label_skips ALTER COLUMN attempt_count SET DEFAULT 1;
    ALTER TABLE outcome_label_skips ALTER COLUMN attempt_count SET NOT NULL;
    UPDATE outcome_label_skips
      SET retry_after=CASE
        WHEN reason='price_provider_unavailable' AND event_ts IS NOT NULL THEN event_ts + interval '4 days'
        WHEN reason='no_ref_price' AND ticker IS NOT NULL AND btrim(ticker)<>''
          AND upper(ticker) NOT IN ('UNKNOWN','SCAN') THEN now() + interval '1 hour'
        WHEN reason ~* '(rate.?limit|429|timeout|timed out|abort)' THEN now() + interval '1 hour'
        ELSE retry_after
      END
      WHERE retry_after IS NULL AND attempt_count < 5;
    UPDATE outcome_label_skips
      SET reason='missing_ticker', retry_after=NULL
      WHERE reason='no_ref_price' AND (ticker IS NULL OR btrim(ticker)='' OR upper(ticker)='UNKNOWN');
    UPDATE outcome_label_skips
      SET reason='unsupported_symbol_or_date', retry_after=NULL
      WHERE reason='no_ref_price' AND upper(ticker)='SCAN';
    CREATE INDEX IF NOT EXISTS idx_outcome_label_skips_retry
      ON outcome_label_skips(retry_after)
      WHERE retry_after IS NOT NULL AND attempt_count < 5;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS max_favorable NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS max_adverse NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS mae_r NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS mfe_r NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS intended_entry NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS initial_sl NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS fill_slippage_pct NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS excluded_from_expectancy BOOLEAN DEFAULT false;
    -- Which engine branch produced the signal. A NEW field: setup_type is a
    -- matching key (brain.js requires exact equality; layer1 aggregation has a
    -- >=10 floor), so branch labels must not be crammed into it. Old rows null.
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS engine_branch TEXT;
    UPDATE signal_outcomes o SET measurement_population=CASE
      WHEN o.source_type='rejection' THEN 'NEVER_ENTERED'
      WHEN EXISTS (SELECT 1 FROM trades t WHERE t.scanner=o.scanner AND t.ticker=o.ticker AND t.opened_at BETWEEN o.event_ts-interval '10 minutes' AND o.event_ts+interval '10 minutes') THEN 'ENTERED'
      ELSE 'NEVER_ENTERED' END
    WHERE measurement_population IS NULL;
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
        -- ret_1d IS NOT NULL is load-bearing. A partial insert writes
        -- ref_price/direction/event_ts with NULL returns and records no
        -- skip; testing only for EXISTENCE let such a row permanently
        -- block its own retry, stranding 1,543 rows across five scanners.
        SELECT 1 FROM signal_outcomes o
        WHERE o.source_type=src.source_type AND o.source_id=src.source_id
          AND o.ret_1d IS NOT NULL
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

// Rows holding a signal_outcomes record that were never actually
// labelled. Structurally invisible before this: the candidate predicate
// excluded them, so the labeller could not see them to count them.
// Three honest backlog numbers. The single `unlabeled_outcomes` figure
// counted only rows the labeller had already half-written, so it could
// not see candidates it had never touched -- 1,543 against a real
// backlog near 52,496. never_attempted reuses the candidate source from
// pickOutcomeCandidates so it means "work the labeller would pick up".
//
// The skips pile is TWO populations needing opposite responses, so it is
// counted as two numbers. parked_skips is genuinely stuck -- no retry_after,
// or attempts at the cap -- and needs a human. deferred_skips has a retry
// scheduled and merely waits its turn; it is the pile that should shrink
// once the retry lane reaches upstream. deferred_skips uses the lane's own
// partial-index predicate, so it counts exactly what the lane can act on.
// The two predicates are complements: every skip row lands in exactly one,
// and a row that fell in neither would vanish -- the failure this fixes.
export async function outcomeBacklogCounts() {
  if (!pool) return null;
  const { rows } = await pool.query(`
    WITH src AS (
      SELECT 'signal'::text source_type, id::text source_id,
        NULLIF(data->>'ts','')::timestamptz event_ts FROM signals
      UNION ALL
      SELECT 'rejection'::text source_type, id::text source_id,
        COALESCE(NULLIF(data->>'rejected_time',''), NULLIF(data->>'ts',''))::timestamptz event_ts
      FROM rejections
    )
    SELECT
      (SELECT count(*)::int FROM signal_outcomes WHERE ret_1d IS NULL) AS unlabeled_partial,
      (SELECT count(*)::int FROM outcome_label_skips
        WHERE retry_after IS NULL OR attempt_count >= 5)              AS parked_skips,
      (SELECT count(*)::int FROM outcome_label_skips
        WHERE retry_after IS NOT NULL AND attempt_count < 5)          AS deferred_skips,
      (SELECT count(*)::int FROM src s
         WHERE s.event_ts IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM signal_outcomes o
                            WHERE o.source_type = s.source_type AND o.source_id = s.source_id)
           AND NOT EXISTS (SELECT 1 FROM outcome_label_skips k
                            WHERE k.source_type = s.source_type AND k.source_id = s.source_id)
      ) AS never_attempted
  `);
  return rows[0] || null;
}

export async function unlabeledOutcomeCount() {
  if (!pool) return null;
  const { rows } = await pool.query(
    'SELECT count(*)::int AS n FROM signal_outcomes WHERE ret_1d IS NULL');
  return rows[0]?.n ?? null;
}

async function recordSkip(row, reason, details = {}) {
  const retryAfter = skipRetryAfter(reason, row.event_ts);
  await pool.query(`
    INSERT INTO outcome_label_skips(source_type,source_id,scanner,ticker,event_ts,reason,attempts,attempt_count,retry_after,last_attempt_at,details)
    VALUES($1,$2,$3,$4,$5,$6,1,1,$7,now(),$9)
    ON CONFLICT(source_type,source_id) DO UPDATE
      SET attempts=outcome_label_skips.attempts+1,
          attempt_count=outcome_label_skips.attempt_count+1,
          reason=$6,
          retry_after=CASE
            WHEN outcome_label_skips.attempt_count+1 >= $8 THEN NULL
            ELSE $7
          END,
          last_attempt_at=now(), details=$9
  `, [row.source_type, row.source_id, row.scanner, row.ticker, row.event_ts, reason, retryAfter, MAX_SKIP_ATTEMPTS, details]);
}

async function deferSkipWithoutAttempt(row, reason, retryAfter) {
  const retry = retryAfter instanceof Date && !Number.isNaN(retryAfter.getTime())
    ? retryAfter
    : new Date(Date.now() + 60 * 60 * 1000);
  await pool.query(`
    UPDATE outcome_label_skips
    SET reason=$3,retry_after=$4,last_attempt_at=now()
    WHERE source_type=$1 AND source_id=$2 AND attempt_count < $5
  `, [row.source_type, row.source_id, reason, retry, MAX_SKIP_ATTEMPTS]);
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
  if (pricePack.rateLimited) return { rateLimited: true, rateLimitKind: pricePack.rateLimitKind,
    deferWithoutAttempt: pricePack.deferWithoutAttempt, retryAfter: pricePack.retryAfter };
  const candles = pricePack.candles || [];
  let priceValidated = false;
  let secondarySource = null;
  let primaryValidationPrice = nearestClose(candles, eventTs);
  let secondaryValidationPrice = null;
  let priceDivergencePct = null;
  if (SCANNERS_STOCK.has(scanner) && primaryValidationPrice) {
    const secondary = await fetchYahooCandles(ticker, new Date(eventTs.getTime()-3600000), new Date(eventTs.getTime()+7200000));
    secondarySource = 'yahoo';
    secondaryValidationPrice = nearestClose(secondary.candles || [], eventTs);
    if (secondaryValidationPrice) {
      const comparison=compareSourcePrices(primaryValidationPrice,secondaryValidationPrice);
      priceValidated = comparison.validated;
      priceDivergencePct = comparison.divergence_pct;
      if (comparison.bad_tick) return { skipped: true, reason: 'bad_tick_suspected', details: {
        primary_source: 'fmp', primary_value: primaryValidationPrice, secondary_source: secondarySource,
        secondary_value: secondaryValidationPrice, divergence_pct: +priceDivergencePct.toFixed(6)
      } };
    }
  }
  if (!refPrice && refPriceSource === 'fetched_candle') refPrice = nearestClose(candles, eventTs);
  if (!refPrice) {
    const reason = pricePack.structural
      ? 'unsupported_symbol_or_date'
      : (pricePack.unsupported ? 'price_provider_unavailable' : 'no_ref_price');
    return { skipped: true, reason };
  }

  const p1h = nearestClose(candles, new Date(eventTs.getTime() + 60 * 60 * 1000));
  const p1d = nearestClose(candles, new Date(eventTs.getTime() + 24 * 60 * 60 * 1000));
  const p3d = nearestClose(candles, new Date(eventTs.getTime() + 3 * 24 * 60 * 60 * 1000));
  const tp = num(data.tp ?? data.tp1 ?? data.take_profit_1 ?? data.intended_tp1);
  const sl = num(data.sl ?? data.stop_loss ?? data.intended_sl);
  const barrierOutcome = (tp && sl) ? barrier(candles, eventTs, direction, tp, sl) : null;
  let measurementPopulation = 'NEVER_ENTERED';
  if (row.source_type === 'signal') {
    const entered = await pool.query(`SELECT 1 FROM trades WHERE scanner=$1 AND ticker=$2
      AND opened_at BETWEEN $3::timestamptz-interval '10 minutes' AND $3::timestamptz+interval '10 minutes' LIMIT 1`, [scanner, ticker, eventTs]);
    if (entered.rowCount) measurementPopulation = 'ENTERED';
  }

  await pool.query(`
    INSERT INTO signal_outcomes(source_type,source_id,scanner,ticker,direction,direction_assumed,ref_price,ref_price_source,event_ts,ret_1h,ret_1d,ret_3d,tp_price,sl_price,barrier_outcome,measurement_population,config_hash,price_validated,secondary_source,primary_validation_price,secondary_validation_price,price_divergence_pct,labeled_at)
    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,now())
    ON CONFLICT(source_type,source_id) DO NOTHING
  `, [
    row.source_type, row.source_id, scanner, ticker, direction, directionAssumed, refPrice,
    refPriceSource, eventTs, pctRet(refPrice, p1h, direction), pctRet(refPrice, p1d, direction),
    pctRet(refPrice, p3d, direction), tp, sl, row.source_type === 'rejection' && !(tp && sl) ? null : barrierOutcome,
    measurementPopulation, data.config_hash || null, priceValidated, secondarySource, primaryValidationPrice,
    secondaryValidationPrice, priceDivergencePct
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
      else if (result.rateLimited) {
        run.rate_limited += 1;
        const rlReason = result.rateLimitKind || 'rate_limited';
        if (result.deferWithoutAttempt) await deferSkipWithoutAttempt(row, rlReason, result.retryAfter);
        else await recordSkip(row, rlReason);
      }
      else {
        run.skipped_no_price += 1;
        const reason = result.reason || 'skipped';
        await recordSkip(row, reason, result.details);
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

export async function retrySkippedOutcomes({ scanners = ['comm', 'indices'], limit = 20, fmpProxyFetch } = {}) {
  if (!pool) throw new Error('Postgres is required for outcome labeling');
  await initLayer1();
  const started = new Date();
  const scannerList = (Array.isArray(scanners) ? scanners : String(scanners || '').split(','))
    .map(s => String(s || '').trim().toLowerCase())
    .filter(Boolean);
  const { rows } = await pool.query(`
    WITH skipped AS (
      SELECT source_type, source_id
      FROM outcome_label_skips
      WHERE scanner = ANY($1::text[])
      ORDER BY last_attempt_at ASC
      LIMIT $2
    )
    SELECT s.source_type, s.source_id, x.scanner, x.ticker, x.data, x.event_ts
    FROM skipped s
    JOIN LATERAL (
      SELECT scanner, ticker, data, NULLIF(data->>'ts','')::timestamptz event_ts
      FROM signals
      WHERE s.source_type='signal' AND id::text=s.source_id
      UNION ALL
      SELECT scanner, ticker, data, COALESCE(NULLIF(data->>'rejected_time',''), NULLIF(data->>'ts',''))::timestamptz event_ts
      FROM rejections
      WHERE s.source_type='rejection' AND id::text=s.source_id
    ) x ON true
    ORDER BY x.event_ts ASC NULLS LAST
  `, [scannerList, Math.max(1, Number(limit) || 20)]);

  return processSkippedRows(rows, {
    started,
    scanners: scannerList,
    requested: Math.max(1, Number(limit) || 20),
    fmpProxyFetch,
    details: { retry_skips: true, scanners: scannerList }
  });
}

async function processSkippedRows(rows, { started = new Date(), scanners = [], requested = 50, fmpProxyFetch, details = {} } = {}) {
  const run = { started_at: started.toISOString(), scanners, requested, attempted: rows.length, labeled: 0, skipped_no_price: 0, rate_limited: 0, details: {} };
  const labeledIds = [];
  for (const row of rows) {
    try {
      const result = await labelRow(row, fmpProxyFetch);
      if (result.labeled) {
        run.labeled += 1;
        labeledIds.push([row.source_type, row.source_id]);
        await pool.query('DELETE FROM outcome_label_skips WHERE source_type=$1 AND source_id=$2', [row.source_type, row.source_id]);
      } else if (result.rateLimited) {
        run.rate_limited += 1;
        const rlReason = result.rateLimitKind || 'rate_limited';
        if (result.deferWithoutAttempt) await deferSkipWithoutAttempt(row, rlReason, result.retryAfter);
        else await recordSkip(row, rlReason);
      } else {
        run.skipped_no_price += 1;
        const reason = result.reason || 'skipped';
        run.details[reason] = (run.details[reason] || 0) + 1;
        await recordSkip(row, reason, result.details);
      }
    } catch (err) {
      run.skipped_no_price += 1;
      const key = String(err.message || 'error').slice(0, 120);
      run.details[key] = (run.details[key] || 0) + 1;
      await recordSkip(row, key);
    }
  }
  const { rows: inserted } = await pool.query(
    `INSERT INTO outcome_label_runs(started_at,finished_at,labeled,skipped_no_price,rate_limited,details)
     VALUES($1,now(),$2,$3,$4,$5) RETURNING *`,
    [started, run.labeled, run.skipped_no_price, run.rate_limited, { ...run.details, ...details, attempted: rows.length }]
  );
  lastRun = inserted[0];
  return { ...run, finished_at: lastRun.finished_at, outcome_label_run_id: lastRun.id, labeled_ids: labeledIds.map(([source_type, source_id]) => ({ source_type, source_id })) };
}

export async function retryDueSkippedOutcomes({ limit = 50, fmpProxyFetch, backfillPausedUntil } = {}) {
  if (!pool) throw new Error('Postgres is required for outcome labeling');
  await initLayer1();
  // The lane runs seconds after the main pass, so it lands inside any pause
  // that pass just armed. Without this it selects 50 DUE rows, gets a local
  // 429 for each, and pushes their retry_after back out -- zero upstream
  // calls and 50 rows deferred for nothing. Skip the run instead; the rows
  // stay due and are picked up by the first cycle after the pause lifts.
  const pausedUntil = typeof backfillPausedUntil === 'function'
    ? Number(backfillPausedUntil()) || 0 : 0;
  if (pausedUntil > Date.now()) {
    return { labeled: 0, skipped_no_price: 0, rate_limited: 0, attempted: 0,
      skipped_backfill_paused: true,
      paused_until: new Date(pausedUntil).toISOString(),
      details: { retry_due_skips: true, skipped_backfill_paused: true } };
  }
  const requested = Math.min(50, Math.max(1, Number(limit) || 50));
  const { rows } = await pool.query(`
    WITH due AS (
      SELECT source_type,source_id
      FROM outcome_label_skips
      WHERE retry_after <= now() AND attempt_count < $1
      ORDER BY retry_after ASC,last_attempt_at ASC
      LIMIT $2
    )
    SELECT d.source_type,d.source_id,x.scanner,x.ticker,x.data,x.event_ts
    FROM due d
    JOIN LATERAL (
      SELECT scanner,ticker,data,NULLIF(data->>'ts','')::timestamptz event_ts
      FROM signals WHERE d.source_type='signal' AND id::text=d.source_id
      UNION ALL
      SELECT scanner,ticker,data,COALESCE(NULLIF(data->>'rejected_time',''),NULLIF(data->>'ts',''))::timestamptz event_ts
      FROM rejections WHERE d.source_type='rejection' AND id::text=d.source_id
    ) x ON true
    ORDER BY x.event_ts ASC NULLS LAST
  `, [MAX_SKIP_ATTEMPTS, requested]);
  return processSkippedRows(rows, {
    requested,
    fmpProxyFetch,
    details: { retry_due_skips: true }
  });
}

export async function runHourlyOutcomeLabeler({ limit = 200, retryLimit = 50, fmpProxyFetch, backfillPausedUntil } = {}) {
  const backfill = await runOutcomeBackfill({ limit, fmpProxyFetch });
  const retries = await retryDueSkippedOutcomes({ limit: retryLimit, fmpProxyFetch, backfillPausedUntil });
  const result = { backfill, retries };
  console.log('[label-outcomes-hourly]', JSON.stringify(result));
  return result;
}

function asAdminQuery(req) {
  return [String(req.query.scanner || '').toLowerCase(), Math.max(1, Number(req.query.days || 30) || 30)];
}

export function attachLayer1(app, db, { adminOnly, fmpProxyFetch, backfillPausedUntil }) {
  app.post('/api/intelligence/outcomes/backfill', adminOnly, async (req, res) => {
    try { res.json(await runOutcomeBackfill({ limit: Number(req.body?.limit || 200), fmpProxyFetch })); }
    catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.post('/api/intelligence/outcomes/retry-skips', adminOnly, async (req, res) => {
    try {
      res.json(await retrySkippedOutcomes({
        scanners: req.body?.scanners || ['comm', 'indices'],
        limit: Number(req.body?.limit || 20),
        fmpProxyFetch
      }));
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.get('/api/intelligence/outcomes', adminOnly, async (req, res) => {
    try {
      const [scanner, days] = asAdminQuery(req);
      const params = [days];
      let filter = `event_ts >= now() - ($1 || ' days')::interval`;
      if (scanner) { params.push(scanner); filter += ` AND scanner=$2`; }
      const { rows } = await pool.query(`
        SELECT source_type, count(*)::int n, round(avg(ret_1d)::numeric,6) avg_ret_1d
        FROM signal_outcomes o WHERE ${filter} AND ${analyticsFirewallSql('o')} GROUP BY source_type ORDER BY source_type
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
    setInterval(() => runHourlyOutcomeLabeler({ limit: 200, retryLimit: 50, fmpProxyFetch, backfillPausedUntil }).catch(e => console.error('[label-outcomes]', e.message)), 60 * 60 * 1000).unref?.();
  }
}

export function computeFillSlippage(direction, entry, intendedEntry) {
  entry = num(entry); intendedEntry = num(intendedEntry);
  if (!entry || !intendedEntry) return null;
  const d = normalizeDirection(direction) || 'LONG';
  const raw = d === 'SHORT' ? (intendedEntry - entry) / intendedEntry : (entry - intendedEntry) / intendedEntry;
  return +(raw * 100).toFixed(6);
}

export function applyTradePricePath(t, currentPrice, onRejected = null) {
  const p = num(currentPrice);
  const entry = num(t?.entry ?? t?.entry_price);
  if (!t || !p || !entry) return false;
  if (p < entry / 5 || p > entry * 5) {
    const rejection = { ts: new Date().toISOString(), trade_id: t.id ?? t.deal_id ?? null, ticker: t.ticker ?? null, price: p, entry, reason: 'PRICE_OUTSIDE_5X_SANITY_BAND' };
    t.rejected_price_ticks = [...(Array.isArray(t.rejected_price_ticks) ? t.rejected_price_ticks : []), rejection].slice(-20);
    onRejected?.(rejection);
    return false;
  }
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
