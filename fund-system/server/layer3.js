import pg from 'pg';
import { analyticsFirewallSql } from './analytics-firewall.js';

const { Pool } = pg;
const DATABASE_URL = process.env.DATABASE_URL || '';
const pool = DATABASE_URL ? new Pool({ connectionString: DATABASE_URL, max: Number(process.env.LAYER3_POOL_MAX || 2), application_name: 'fund-system-layer3' }) : null;

const SCANNERS = ['fmp', 'fmp_alpaca', 'forex', 'comm', 'pa', 'failed_breakout', 'volume', 'mean_reversion', 'indices', 'crypto'];
let dailyStarted = false;
let lastHealthJobDate = '';
let lastEquityJobDate = '';
let lastAutoHaltDate = '';
let lastRiskMult = null;

function num(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function tag(v) {
  return String(v || '').trim().toLowerCase();
}

function normalizeDirection(v) {
  const d = String(v || '').trim().toUpperCase();
  if (['SELL', 'SHORT'].includes(d)) return 'SHORT';
  return 'LONG';
}

function dataTs(row) {
  const raw = row?.closed_at || row?.data?.closed_at || row?.ts || row?.data?.ts || row?.opened_at || row?.data?.opened_at;
  const d = raw ? new Date(raw) : null;
  return d && Number.isFinite(d.getTime()) ? d : null;
}

function isTestTrade(t) {
  const scanner = tag(t.scanner || t.data?.scanner);
  const ticker = String(t.ticker || t.data?.ticker || '').toUpperCase();
  return scanner === 'test' || scanner === 'test_harness' || ticker.startsWith('ZZ') || String(t.deal_id || '').startsWith('L3_');
}

function rMultiple(t) {
  const existing = num(t.r_multiple ?? t.data?.r_multiple);
  if (existing !== null) return existing;
  const pnl = num(t.pnl_net ?? t.data?.pnl_net ?? t.pnl ?? t.data?.pnl);
  const risk = num(t.risk_amount ?? t.data?.risk_amount);
  if (pnl === null || !risk) return null;
  return pnl / Math.abs(risk);
}

function expectancy(rows) {
  const r = rows.map(rMultiple).filter(v => v !== null);
  const wins = r.filter(v => v > 0);
  const losses = r.filter(v => v <= 0);
  return {
    n: r.length,
    expectancy_r: r.length ? +(r.reduce((s, v) => s + v, 0) / r.length).toFixed(4) : null,
    win_rate: r.length ? +(wins.length / r.length * 100).toFixed(2) : null,
    avg_win_r: wins.length ? +(wins.reduce((s, v) => s + v, 0) / wins.length).toFixed(4) : null,
    avg_loss_r: losses.length ? +(losses.reduce((s, v) => s + v, 0) / losses.length).toFixed(4) : null
  };
}

async function scannerOutputSince(_db, scanner, days) {
  const {rows}=await pool.query(`SELECT max(ts) last_output FROM (
    SELECT NULLIF(data->>'ts','')::timestamptz ts FROM signals WHERE scanner=$1
    UNION ALL SELECT COALESCE(NULLIF(data->>'rejected_time',''),NULLIF(data->>'ts',''))::timestamptz FROM rejections r WHERE scanner=$1 AND ${analyticsFirewallSql('','r')}
    UNION ALL SELECT opened_at FROM trades WHERE scanner=$1
  ) x`,[scanner]);
  const last=rows[0]?.last_output?new Date(rows[0].last_output):null;
  return !!(last&&Date.now()-last.getTime()<=days*86400000);
}

function heartbeatAlive(db, scanner) {
  const hb = db.data.heartbeats?.[scanner];
  if (!hb) return false;
  const ts = Number(hb.ts) || Date.parse(hb.ts || hb.iso || '');
  return Number.isFinite(ts) && Date.now() - ts < 30 * 60_000;
}

// L3: these were unanchored substrings, so any ticker CONTAINING the fragment
// matched -- DOW (Dow Inc) classified as an index, SOLV/ETHA/SOLS as crypto,
// anything containing OIL as a commodity. This feeds the 1.5x correlation
// multiplier and the 5-position concentration cap, so a misclassification
// silently distorts both.
//
// Now exact-match sets plus anchored patterns, seeded from the DOMAIN table in
// scripts/test-suite-v3.cjs AND from the tickers the live scanners actually
// trade. DOMAIN alone was insufficient: production indices use DE40, EU50,
// AU200, J225 and US100, none of which DOMAIN matches.
//
// DOW is deliberately NOT an index: the Dow index trades here as US30, while
// DOW is Dow Inc, a US stock.
const CRYPTO_QUOTE_SUFFIX = /(USDT|USDC|BUSD)$/;          // anchored; NOT bare USD (that would eat EURUSD)
const CRYPTO_BASE_EXACT = new Set([
  'BTC','ETH','SOL','XRP','ADA','DOGE','BNB','TRX','UNI','AAVE','LTC','DOT','AVAX','LINK','MATIC','ZEC'
]);
const FOREX_PAIR = /^(EUR|GBP|USD|JPY|AUD|NZD|CAD|CHF)(USD|EUR|GBP|JPY|AUD|NZD|CAD|CHF)$/;
const FOREX_SLASHED = /^[A-Z]{3}\/[A-Z]{3}$/;
const COMMODITY_EXACT = new Set([
  'GOLD','SILVER','USOIL','OIL_CRUDE','NATURALGAS','NATGAS','COPPER','PLATINUM','PALLADIUM',
  'BRENT','WTI','XAUUSD','XAGUSD'
]);
const INDEX_EXACT = new Set([
  'US500','US30','US100','UK100','GER40','GERMANY40','DE40','JP225','J225','FRA40','EU50',
  'AUS200','AU200','HK50','SPX','NDX','DJI','SPY','QQQ','DAX','FTSE','NAS100'
]);

export function classifyAssetClass(ticker, scanner = '') {
  const s = tag(scanner);
  const t = String(ticker || '').trim().toUpperCase();
  // Scanner tag is authoritative when present -- it is set by the scanner
  // itself and is not guessable from the symbol.
  if (s === 'crypto') return 'crypto';
  if (s === 'forex') return 'forex';
  if (s === 'comm') return 'commodity';
  if (s === 'indices') return 'index';
  // Ticker-only fallback (e.g. ledger rows with no scanner recorded).
  if (CRYPTO_QUOTE_SUFFIX.test(t) || CRYPTO_BASE_EXACT.has(t)) return 'crypto';
  if (FOREX_PAIR.test(t) || FOREX_SLASHED.test(t)) return 'forex';
  if (COMMODITY_EXACT.has(t)) return 'commodity';
  if (INDEX_EXACT.has(t)) return 'index';
  return 'us_stock';
}

export function correlationHeat(ledger = [], proposed = {}, accountSize = 10000) {
  const proposedClass = classifyAssetClass(proposed.ticker, proposed.scanner);
  const proposedDirection = normalizeDirection(proposed.direction);
  const risk = Math.max(0, Number(proposed.risk_amount) || 0);
  let effectiveRisk = risk;
  let rawRisk = risk;
  let classDirectionCount = 0;
  const rows = [];
  for (const p of ledger || []) {
    const assetClass = classifyAssetClass(p.ticker, p.scanner);
    const direction = normalizeDirection(p.direction);
    const r = Math.max(0, Number(p.risk_amount) || 0);
    const related = assetClass === proposedClass && direction === proposedDirection;
    if (related) classDirectionCount += 1;
    const multiplier = related ? 1.5 : 1.0;
    rawRisk += r;
    effectiveRisk += r * multiplier;
    rows.push({ deal_id: p.deal_id, ticker: p.ticker, scanner: p.scanner, asset_class: assetClass, direction, risk_amount: r, multiplier, effective_risk: +(r * multiplier).toFixed(4) });
  }
  return {
    asset_class: proposedClass,
    direction: proposedDirection,
    class_direction_count: classDirectionCount,
    raw_heat_pct: accountSize ? +(rawRisk / accountSize * 100).toFixed(2) : 0,
    effective_heat_pct: accountSize ? +(effectiveRisk / accountSize * 100).toFixed(2) : 0,
    effective_risk: +effectiveRisk.toFixed(4),
    arithmetic: rows,
    proposed_risk: risk
  };
}

export async function initLayer3() {
  if (!pool) return false;
  await pool.query(`
    CREATE TABLE IF NOT EXISTS strategy_health (
      id BIGSERIAL PRIMARY KEY,
      computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      scanner TEXT,
      window_trades INTEGER DEFAULT 0,
      expectancy_r NUMERIC,
      win_rate NUMERIC,
      avg_win_r NUMERIC,
      avg_loss_r NUMERIC,
      baseline_expectancy_r NUMERIC,
      state TEXT NOT NULL CHECK (state IN ('HEALTHY','DECAYING','HALT_RECOMMENDED','INSUFFICIENT_DATA','ZOMBIE'))
    );
    CREATE INDEX IF NOT EXISTS idx_strategy_health_latest ON strategy_health(computed_at DESC, scanner);
    CREATE TABLE IF NOT EXISTS equity_curve (
      date DATE PRIMARY KEY,
      equity NUMERIC NOT NULL,
      high_water_mark NUMERIC NOT NULL
    );
    ALTER TABLE risk_ledger ADD COLUMN IF NOT EXISTS direction TEXT;
    ALTER TABLE risk_ledger ADD COLUMN IF NOT EXISTS asset_class TEXT;
  `);
  return true;
}

async function recentStateCount(scanner, state, limitDays = 10) {
  const { rows } = await pool.query(`
    SELECT state
    FROM strategy_health
    WHERE scanner=$1
    ORDER BY computed_at DESC
    LIMIT $2
  `, [scanner, limitDays]);
  let count = 0;
  for (const row of rows) {
    if (row.state !== state) break;
    count += 1;
  }
  return count;
}

export async function runStrategyHealth(db, { sendTelegramAlert, save, simulateAutoHalt = false } = {}) {
  if (!pool) throw new Error('Postgres is required for strategy health');
  await initLayer3();
  const computedAt = new Date();
  const trades = (db.data.trades || [])
    .filter(t => String(t.status || '').toUpperCase() === 'CLOSED')
    .filter(t => (t.pnl_net ?? t.pnl) !== null && (t.pnl_net ?? t.pnl) !== undefined)
    .filter(t => t.excluded_from_expectancy !== true && t.data?.excluded_from_expectancy !== true)
    .filter(t => !isTestTrade(t));
  const rows = [];
  for (const scanner of SCANNERS) {
    const st = trades.filter(t => tag(t.scanner || t.data?.scanner) === scanner).sort((a, b) => (dataTs(b)?.getTime() || 0) - (dataTs(a)?.getTime() || 0));
    const windowRows = st.slice(0, 30);
    const baselineRows = st.filter(t => {
      const d = dataTs(t);
      return d && computedAt - d <= 90 * 86400000;
    });
    const win = expectancy(windowRows);
    const base = expectancy(baselineRows);
    let state = 'HEALTHY';
    if (st.length < 20) state = heartbeatAlive(db, scanner) && !(await scannerOutputSince(db, scanner, 3)) ? 'ZOMBIE' : 'INSUFFICIENT_DATA';
    else if (win.n >= 30 && win.expectancy_r < 0 && await recentStateCount(scanner, 'HALT_RECOMMENDED', 9) >= 9) state = 'HALT_RECOMMENDED';
    else if (base.expectancy_r > 0 && win.expectancy_r !== null && win.expectancy_r < base.expectancy_r * 0.5 && await recentStateCount(scanner, 'DECAYING', 4) >= 4) state = 'DECAYING';

    const result = await pool.query(`
      INSERT INTO strategy_health(computed_at,scanner,window_trades,expectancy_r,win_rate,avg_win_r,avg_loss_r,baseline_expectancy_r,state)
      VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
      RETURNING *
    `, [computedAt, scanner, win.n, win.expectancy_r, win.win_rate, win.avg_win_r, win.avg_loss_r, base.expectancy_r, state]);
    rows.push(result.rows[0]);
  }
  const haltRows = rows.filter(r => r.state === 'HALT_RECOMMENDED');
  const today = computedAt.toISOString().slice(0, 10);
  const oldPaperOnly = db.data.scanner_config?.paper_only === true;
  if (haltRows.length && !oldPaperOnly && lastAutoHaltDate !== today) {
    const scanners = haltRows.map(r => r.scanner).join(',');
    db.data.scanner_config = db.data.scanner_config || {};
    db.data.scanner_config.paper_only = true;
    db.data.scanner_config.updated_at = computedAt.toISOString();
    await pool.query(`
      INSERT INTO intelligence_changelog(scanner, parameter, old_value, new_value, reason, approved_by)
      VALUES($1,'paper_only',$2,'true',$3,'auto-protective')
    `, [scanners, String(oldPaperOnly), `HALT_RECOMMENDED strategy health: ${scanners}`]);
    if (save) await save();
    if (sendTelegramAlert) await sendTelegramAlert(`${simulateAutoHalt ? '[TEST] ' : ''}HALT_RECOMMENDED: ${scanners}. paper_only=true set by auto-protective health monitor.`);
    lastAutoHaltDate = today;
  }
  return { computed_at: computedAt.toISOString(), rows, auto_halt: haltRows.map(r => r.scanner) };
}

export async function latestStrategyHealth() {
  if (!pool) throw new Error('Postgres is required for strategy health');
  const { rows: latest } = await pool.query('SELECT max(computed_at) computed_at FROM strategy_health');
  const ts = latest[0]?.computed_at;
  if (!ts) return { computed_at: null, rows: [] };
  const { rows } = await pool.query(`
    SELECT scanner, window_trades, expectancy_r, win_rate, avg_win_r, avg_loss_r, baseline_expectancy_r, state, computed_at
    FROM strategy_health
    WHERE computed_at=$1
    ORDER BY array_position($2::text[], scanner)
  `, [ts, SCANNERS]);
  return { computed_at: ts, rows };
}

export async function runEquityCurve(db) {
  if (!pool) throw new Error('Postgres is required for equity curve');
  await initLayer3();
  const accountSize = Number(db.data.scanner_config?.account_size || db.data.fund?.total_account_size || 10000);
  const pnl = (db.data.trades || [])
    .filter(t => String(t.status || '').toUpperCase() === 'CLOSED')
    .filter(t => t.excluded_from_expectancy !== true && t.data?.excluded_from_expectancy !== true)
    .filter(t => !isTestTrade(t))
    .reduce((s, t) => s + (Number(t.pnl_net ?? t.pnl) || 0), 0);
  const equity = accountSize + pnl;
  const today = new Date().toISOString().slice(0, 10);
  const previous = await pool.query('SELECT max(high_water_mark)::float8 hwm FROM equity_curve');
  const hwm = Math.max(accountSize, Number(previous.rows[0]?.hwm || 0), equity);
  await pool.query(`
    INSERT INTO equity_curve(date,equity,high_water_mark)
    VALUES($1,$2,$3)
    ON CONFLICT(date) DO UPDATE SET equity=$2, high_water_mark=greatest(equity_curve.high_water_mark,$3)
  `, [today, equity, hwm]);
  // A present-but-wrong account_size used to read as fresh: the old flag
  // fired only when the value was ABSENT. It now compares against the
  // observed broker balance and how long ago that was observed.
  const bb = db.data.broker_balance || null;
  const observed = bb && Number.isFinite(Number(bb.balance)) ? Number(bb.balance) : null;
  const ageMs = bb?.observed_at ? Date.now() - Date.parse(bb.observed_at) : NaN;
  const divergencePct = observed ? +(((accountSize - observed) / observed) * 100).toFixed(2) : null;
  const stale =
    !db.data.scanner_config?.account_size ? 'not_configured'
    : observed === null                   ? 'no_broker_observation'
    : !Number.isFinite(ageMs) || ageMs > 6 * 60 * 60_000 ? 'observation_stale'
    : Math.abs(divergencePct) > 5         ? 'diverged_from_broker'
    : false;
  return { date: today, equity, high_water_mark: hwm, account_size: accountSize,
           broker_balance: observed, divergence_pct: divergencePct,
           account_size_stale: stale };
}

export async function drawdownState() {
  if (!pool) return { drawdown_pct: 0, risk_mult: 1, equity: null, high_water_mark: null };
  await initLayer3();
  const { rows } = await pool.query('SELECT date,equity::float8,high_water_mark::float8 FROM equity_curve ORDER BY date DESC LIMIT 1');
  const row = rows[0];
  if (!row || !row.high_water_mark) return { drawdown_pct: 0, risk_mult: 1, equity: null, high_water_mark: null };
  const dd = Math.max(0, (row.high_water_mark - row.equity) / row.high_water_mark * 100);
  const riskMult = dd > 10 ? 0.5 : dd >= 5 ? 0.75 : 1.0;
  return { date: row.date, equity: row.equity, high_water_mark: row.high_water_mark, drawdown_pct: +dd.toFixed(2), risk_mult: riskMult };
}

export async function maybeAlertRiskMult(sendTelegramAlert) {
  const state = await drawdownState();
  if (lastRiskMult === null) {
    lastRiskMult = state.risk_mult;
    return { sent: false, primed: true, ...state };
  }
  if (state.risk_mult !== lastRiskMult) {
    const previous = lastRiskMult;
    lastRiskMult = state.risk_mult;
    const telegram = sendTelegramAlert ? await sendTelegramAlert(`Drawdown throttle changed: risk_mult ${previous} -> ${state.risk_mult}; drawdown=${state.drawdown_pct}%`) : null;
    return { sent: true, previous, telegram, ...state };
  }
  return { sent: false, ...state };
}

export function startLayer3Jobs(db, { sendTelegramAlert, save }) {
  if (!pool || dailyStarted || process.env.DISABLE_LAYER3_JOBS === 'true') return;
  dailyStarted = true;
  setInterval(() => {
    const now = new Date();
    const date = now.toISOString().slice(0, 10);
    if (now.getUTCHours() >= 22 && lastHealthJobDate !== date) {
      lastHealthJobDate = date;
      runStrategyHealth(db, { sendTelegramAlert, save }).catch(e => console.error('[strategy-health]', e.message));
    }
    if (now.getUTCHours() >= 22 && lastEquityJobDate !== date) {
      lastEquityJobDate = date;
      runEquityCurve(db).then(() => maybeAlertRiskMult(sendTelegramAlert)).catch(e => console.error('[equity-curve]', e.message));
    }
  }, 15 * 60_000).unref?.();
}

export async function insertSimEquity({ equity, high_water_mark }) {
  if (!pool) throw new Error('Postgres is required for simulation');
  const date = '2099-12-31';
  await pool.query('INSERT INTO equity_curve(date,equity,high_water_mark) VALUES($1,$2,$3) ON CONFLICT(date) DO UPDATE SET equity=$2, high_water_mark=$3', [date, equity, high_water_mark]);
  return date;
}

export async function cleanLayer3Artifacts() {
  if (!pool) return;
  await pool.query(`DELETE FROM equity_curve WHERE date='2099-12-31'`);
  await pool.query(`DELETE FROM strategy_health WHERE scanner LIKE 'l3_%' OR scanner='fake_l3'`);
  await pool.query(`DELETE FROM risk_ledger WHERE deal_id LIKE 'L3_%'`);
  await pool.query(`DELETE FROM rejections WHERE id::text LIKE 'L3_%' OR ticker LIKE 'L3_%' OR data->>'deal_id' LIKE 'L3_%'`);
}
