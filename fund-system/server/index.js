import express from 'express';
import cors from 'cors';
import crypto from 'crypto';
import path from 'path';
import { fileURLToPath } from 'url';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';
import { JSONFilePreset } from 'lowdb/node';
import * as fmpStore from './fmp-budget.js';
import attachScoring from './scoring-endpoints.cjs';
import {
  attachParamStore,
  getConfigHash,
  getScannerParams,
  initParamStore,
  recordStampViolation, recordConfigChangelog
  ,validateParamValue, SCANNER_ORDER, logChangelogRow
} from './param-store.js';
import {
  applyTradePricePath,
  attachLayer1,
  capitalLogin,
  computeFillSlippage,
  finalizeTradePath,
  invalidateCapitalSession,
  unlabeledOutcomeCount,
  outcomeBacklogCounts,
  initLayer1
} from './layer1.js';
import {
  attachLayer2,
  initLayer2
} from './layer2.js';
import {
  brainShadowStats,
  brainStatsText,
  brainFeatureVector,
  similarOutcomeVerdict
} from './brain.js';
import {
  classifyAssetClass,
  cleanLayer3Artifacts,
  correlationHeat,
  drawdownState,
  initLayer3,
  insertSimEquity,
  latestStrategyHealth,
  maybeAlertRiskMult,
  runEquityCurve,
  runStrategyHealth,
  startLayer3Jobs
} from './layer3.js';
import {
  attachLayer4,
  initLayer4,
  latestRegimeSnapshots,
  startLayer4Jobs
} from './layer4.js';
import { shapeRegimeLatest } from './regime-latest.js';
import { computeFleetState, readActualFleet, fleetWarnings } from './fleet-state.js';
import { reconcilePositions } from './position-reconcile.js';
import { getFleetExpected, initFleetStore } from './fleet-store.js';
import { attachBrokerResolve } from './broker-resolve.js';
import { resolveCloseEconomics } from './close-economics.js';
import { attachScoreboard, buildScoreboard, scannerDigestLines } from './scoreboard.js';
import { attachAccountSize } from './account-size.js';
import { telemetryGaps } from './telemetry-gap.js';
import { computeParamConnectivity, connectivityCacheAge } from './param-connectivity.js';
import {
  attachLayer5,
  initLayer5,
  startLayer5Jobs
} from './layer5.js';
import { attachEarningsGuard, guardResult, initEarningsGuard, startEarningsGuardJobs } from './earnings-guard.js';
import { attachExperiments, initExperiments, startExperimentJobs } from './intelligence-experiments.js';
import { attachPositionWatchdog, startPositionWatchdogs } from './position-watchdog.js';
import { computeNetPnl, financingDigestLines, initFinancing, runFinancing, startFinancingJob } from './financing.js';
import { attachEventJournal, initEventJournal, journalEvent, scannerErrorStats } from './event-journal.js';
import { startLatencyMonitor } from './latency-monitor.js';
import { findTradeForMutation } from './trade-match.js';
import {
  loadFromPostgres,
  saveToPostgres,
  postgresEnabled,
  dualWriteEnabled,
  upsertHeartbeatPostgres,
  insertPingPostgres,
  insertSignalPostgres,
  insertRejectionPostgres,
  insertUpdatePostgres,
  upsertTradePostgres,
  insertBrainPostgres,
  insertReservationPostgres,
  sweepExpiredReservationsPostgres,
  openPositionPostgres,
  closePositionPostgres
} from './storage-adapter.js';
import fundIntegrity from './fund-integrity.cjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app  = express();
const PORT = process.env.PORT || 3210;
if (!process.env.ADMIN_PIN) throw new Error('ADMIN_PIN is required; refusing to boot with a default PIN');

const corsOrigins = (process.env.CORS_ORIGINS || '')
  .split(',')
  .map(v => v.trim())
  .filter(Boolean);
app.use(cors(corsOrigins.length ? {
  origin(origin, callback) {
    if (!origin || corsOrigins.includes(origin)) return callback(null, true);
    return callback(new Error('Origin not allowed'));
  }
} : undefined));
app.use(express.json({ limit: '10mb' }));
app.use(express.static(path.join(__dirname, '../public')));
app.use((req,res,next)=>{
  if (!/(stats|scorecard|intelligence)/i.test(req.path)) return next();
  const original=res.json.bind(res);
  const stamp=value=>{
    if(Array.isArray(value))return value.map(stamp);
    if(!value||typeof value!=='object')return value;
    const out=Object.fromEntries(Object.entries(value).map(([k,v])=>[k,stamp(v)]));
    const n=[out.n,out.count,out.samples,out.n_labeled,out.window_trades].find(Number.isFinite);
    if(n!==undefined)out.too_thin=Number(n)<30;
    return out;
  };
  res.json=payload=>original(stamp(payload));next();
});

// ── Request logging ──────────────────────────────────────────
app.use((req, res, next) => {
  const start = Date.now();
  res.on('finish', () => {
    const ms = Date.now() - start;
    console.log(`${new Date().toISOString()}  ${req.method} ${req.path}  ${res.statusCode}  ${ms}ms  ${req.ip || '-'}`);
  });
  next();
});

// ── DB ───────────────────────────────────────────────────────
const DB_PATH = process.env.DB_PATH || path.join(__dirname, '../data/fund.json');
mkdirSync(path.dirname(DB_PATH), { recursive: true });
const DATA_DIR = path.dirname(DB_PATH);
const LOCAL_ENV_PATH = path.join(__dirname, '../.env');

function readLocalEnvValue(key) {
  try {
    if (!existsSync(LOCAL_ENV_PATH)) return '';
    const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = readFileSync(LOCAL_ENV_PATH, 'utf8').match(new RegExp(`^${escaped}=(.*)$`, 'm'));
    return match ? match[1].trim().replace(/^['"]|['"]$/g, '') : '';
  } catch {
    return '';
  }
}

function envValue(...keys) {
  for (const key of keys) {
    const value = process.env[key] || readLocalEnvValue(key);
    if (value) return value;
  }
  return '';
}

async function sendTelegramAlert(text) {
  const token = envValue('TELEGRAM_BOT_TOKEN', 'TG_BOT_TOKEN');
  const chatId = envValue('TELEGRAM_CHAT_ID', 'TG_CHAT_ID');
  if (!token || !chatId) {
    return { ok: false, skipped: 'telegram credentials missing' };
  }
  const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: chatId,
      text,
      disable_web_page_preview: true
    })
  });
  const bodyText = await response.text();
  let body;
  try { body = bodyText ? JSON.parse(bodyText) : null; } catch { body = bodyText; }
  return { ok: response.ok, status: response.status, body };
}
fundIntegrity.validateFundFileOnLoad(DB_PATH);

const defaultData = {
  // Fund config
  fund: {
    name: 'Alpha Bot Fund',
    base_currency: 'GBP',
    performance_fee_pct: 20,   // % of profits taken as fee
    management_fee_pct: 0,     // monthly % (usually 0 for friends)
    high_watermark: true,
    total_account_size: 10000, // your Capital.com account size
    created_at: new Date().toISOString()
  },
  // Investors
  investors: [],
  // Stakes / capital events
  stakes: [],
  // Allocations per investor per scanner
  allocations: [],
  // Risk settings per investor
  risk_settings: [],
  // Trades from scanner (received via webhook)
  trades: [],
  signals: [],
  rejections: [],
  updates: [],
  pings: [],
  // Withdrawals & fees
  withdrawals: [],
  fees: [],
  // Monthly snapshots for charting
  monthly_snapshots: [],
  // Sessions
  sessions: [],
  // ── Centralized config — single source of truth for all scanners ──
  // Scanners fetch this at startup instead of hardcoding values
  scanner_config: {
    vps_url: 'http://72.62.134.167:3210',
    capital_base_url: 'https://demo-api-capital.backend-capital.com',
    account_size: 10000,
    gemini_model: 'gemini-2.5-flash',
    // Global risk caps — enforced across ALL scanners combined
    max_global_heat_pct: 20,      // max % of account at risk across all scanners at once
    max_open_positions: 10,        // max simultaneous positions all scanners combined
    kill_switch: false,            // master kill — true halts ALL new orders everywhere
    paper_only: false,             // true = no real orders placed anywhere
    updated_at: new Date().toISOString()
  },
  // ── Global risk ledger — tracks open risk across every scanner ──
  risk_ledger: [],   // [{ scanner, ticker, deal_id, risk_amount, opened_at }]
  // Heartbeats — last-seen time per scanner for dead-scanner detection
  heartbeats: {},    // { fmp: {ts, status, msg}, forex: {...}, ... }
  // ── Trade Brain — knowledge base of every closed trade ──
  // Each record stores the signal's features + how the trade actually went.
  // New signals query this to find similar past trades and their win rate.
  trade_brain: [],    // [{ scanner, setup_type, direction, features:{...}, outcome:{win, r_multiple, pnl}, recorded_at }]
  rejection_analysis: [],
  // Live positions snapshot from scanners
  live_positions: { positions: [], updated_at: null },
  // EOD reports
  eod_reports: [],
  // Capital reallocations
  reallocations: [],
  // System2 scoring ideas
  ideas: [],
  // System2 run metadata
  system2_run_metadata: []
};

const db = await JSONFilePreset(DB_PATH, defaultData);
const storedData = db.data || {};
db.data = {
  ...defaultData,
  ...storedData,
  fund: { ...defaultData.fund, ...(storedData.fund || {}) },
  scanner_config: { ...defaultData.scanner_config, ...(storedData.scanner_config || {}) }
};
for (const [key, value] of Object.entries(defaultData)) {
  if (db.data[key] === null || db.data[key] === undefined) {
    db.data[key] = Array.isArray(value) ? [] : { ...value };
  }
}

// ── Storage mode: Postgres / JSON / dual-write ──
// If USE_POSTGRES=true, load the dataset from Postgres into the in-memory db.data
// that the rest of the server already uses. Falls back to JSON on any error.
// Set for the LIFE OF THE PROCESS when the Postgres load falls back to JSON.
// It never clears: a fallback that happened at boot is still true an hour
// later, and the mirror stays the in-memory source until the next restart.
let postgresLoadFailure = null;
let postgresLoadAlerted = false;
if (postgresEnabled) {
  try {
    const pgData = await loadFromPostgres();
    if (pgData) {
      // Merge: JSON data first, then Postgres overwrites known tables,
      // but preserves collections that only exist in JSON (live_positions, ideas, etc.)
      db.data = { ...defaultData, ...db.data, ...pgData };
      console.log('✓ Loaded data from Postgres');
      if (dualWriteEnabled) console.log('  (dual-write ON — also writing fund.json as safety net)');
    }
  } catch (e) {
    // Silent fallback was the real defect here, not the missing column. The
    // app switched from source-of-truth to mirror and only err.log knew.
    // NOT now(): this catch runs during module evaluation, before the `now`
    // helper is initialised, and calling it threw a TDZ ReferenceError that
    // CRASHED the process on the very path meant to survive a failure.
    postgresLoadFailure = { at: new Date().toISOString(), error: String(e.message || e).slice(0, 300) };
    console.error('⚠ Postgres load failed, falling back to fund.json:', e.message);
  }
}

try {
  await initParamStore();
  console.log('✓ Parameter store ready');
} catch (e) {
  console.error('⚠ Parameter store init failed:', e.message);
}

try {
  await initLayer1();
  console.log('Layer 1 intelligence ready');
} catch (e) {
  console.error('Layer 1 intelligence init failed:', e.message);
}

try {
  await initLayer2();
  console.log('Layer 2 filter verdicts ready');
} catch (e) {
  console.error('Layer 2 filter verdicts init failed:', e.message);
}

try {
  await initLayer3();
  console.log('Layer 3 strategy health ready');
} catch (e) {
  console.error('Layer 3 init failed:', e.message);
}

try {
  await initLayer4();
  console.log('Layer 4 regime snapshots ready');
} catch (e) {
  console.error('Layer 4 init failed:', e.message);
}

try {
  await initLayer5();
  console.log('Layer 5 weekly analyst ready');
} catch (e) {
  console.error('Layer 5 init failed:', e.message);
}

try {
  await initEarningsGuard();
  console.log('Earnings calendar guard ready');
} catch (e) {
  console.error('Earnings guard init failed:', e.message);
}
try { await initExperiments(); console.log('Change attribution and challenger harness ready'); }
catch(e) { console.error('Experiment engine init failed:',e.message); }
try { await initFinancing(); console.log('Cost-complete financing ready'); }
catch (e) { console.error('Financing init failed:', e.message); }
try { await initEventJournal(); console.log('Append-only event journal ready'); }
catch (e) { console.error('Event journal init failed:', e.message); }

// Seed IDs
const ids = {};
['investors','stakes','allocations','risk_settings','trades','signals',
 'rejections','updates','pings','withdrawals','fees','monthly_snapshots'].forEach(k => {
  const rows = db.data[k] || [];
  ids[k] = rows.length ? Math.max(...rows.map(r => r.id || 0)) + 1 : 1;
});
const nid = k => ids[k]++;

const now    = () => new Date().toISOString();
const today  = () => new Date().toISOString().split('T')[0];
const isToday = ts => ts && ts.startsWith(today());
// save() persistence strategy:
//   - Postgres OFF  → write fund.json (original behaviour)
//   - Postgres ON   → write Postgres (primary). Also write fund.json if DUAL_WRITE=true.
// Primary persistence failures must fail the request. A memory-only risk/config
// mutation is not success and would disappear on restart.
const save = async () => {
  try {
    const openTrades=(db.data.trades||[]).filter(t=>['OPEN','PARTIAL'].includes(t.status));
    const closedTrades=(db.data.trades||[]).filter(t=>!['OPEN','PARTIAL'].includes(t.status)).sort((a,b)=>new Date(b.closed_at||b.ts||0)-new Date(a.closed_at||a.ts||0)).slice(0,5000);
    db.data.trades=[...openTrades,...closedTrades];
    db.data.updates=(db.data.updates||[]).slice(0,5000);
    db.data.sessions=(db.data.sessions||[]).filter(s=>s.expires>Date.now()).slice(-500);
    if (postgresEnabled) {
      await saveToPostgres(db.data);
      if (dualWriteEnabled) { try { await db.write(); } catch(e){ console.error('dual-write json failed:', e.message); } }
    } else {
      await db.write();
    }
  } catch (error) {
    console.error('[PERSISTENCE_FAILURE]', error.message);
    await sendTelegramAlert(`PERSISTENCE FAILURE: ${error.message}. Request rejected; inspect Postgres immediately.`).catch(()=>{});
    throw error;
  }
};

let jsonMirrorTimer = null;
let jsonMirrorInFlight = false;
let jsonMirrorDirty = false;

const scheduleJsonMirror = () => {
  if (!dualWriteEnabled) return;
  jsonMirrorDirty = true;
  if (jsonMirrorTimer || jsonMirrorInFlight) return;
  jsonMirrorTimer = setTimeout(async () => {
    jsonMirrorTimer = null;
    if (jsonMirrorInFlight) return;
    jsonMirrorInFlight = true;
    jsonMirrorDirty = false;
    try {
      await db.write();
    } catch (e) {
      console.error('dual-write json failed:', e.message);
    } finally {
      jsonMirrorInFlight = false;
      if (jsonMirrorDirty) scheduleJsonMirror();
    }
  }, Number(process.env.JSON_MIRROR_DEBOUNCE_MS || 5000));
  jsonMirrorTimer.unref?.();
};

const saveHeartbeat = async (scanner, heartbeat) => {
  if (postgresEnabled) {
    await upsertHeartbeatPostgres(scanner, heartbeat);
    return;
  }
  await db.write();
};

const savePing = async (ping) => {
  if (postgresEnabled) {
    await insertPingPostgres(ping);
    return;
  }
  await db.write();
};

const saveSignalHot = async (signal) => {
  if (postgresEnabled) { await insertSignalPostgres(signal); scheduleJsonMirror(); return; }
  await db.write();
};
const saveRejectionHot = async (rejection) => {
  if (postgresEnabled) { await insertRejectionPostgres(rejection); scheduleJsonMirror(); return; }
  await db.write();
};
const saveUpdateHot = async (update) => {
  if (postgresEnabled) { await insertUpdatePostgres(update); scheduleJsonMirror(); return; }
  await db.write();
};
const saveTradeHot = async (trade) => {
  if (postgresEnabled) { await upsertTradePostgres(trade); scheduleJsonMirror(); return; }
  await db.write();
};
const saveBrainHot = async (brain) => {
  if (postgresEnabled) { await insertBrainPostgres(brain); scheduleJsonMirror(); return; }
  await db.write();
};

const hashPin  = pin => crypto.createHash('sha256').update(pin + 'fund_salt_v1').digest('hex');
const mkToken  = () => crypto.randomBytes(28).toString('hex');
const SESSION_TTL = 14 * 24 * 60 * 60 * 1000;
const SCANNER_API_KEY = process.env.SCANNER_API_KEY || '';
const loginAttempts = new Map();
const rejectionRates = new Map();
// R2 helper: the ET calendar date for a timestamp. Trade timestamps are stored
// as UTC ISO strings; slicing them yields the UTC date, which diverges from the
// ET date between 00:00-04:00 UTC. Returns '' for missing/unparseable input so
// such rows never accidentally match today.
const ET_DATE_FMT = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' });
function etDateOf(ts) {
  if (!ts) return '';
  const d = ts instanceof Date ? ts : new Date(ts);
  return Number.isFinite(d.getTime()) ? ET_DATE_FMT.format(d) : '';
}
const scannerErrorRates = new Map();      // `${scanner}:${minute}` -> {count, dropped}
const scannerErrorTelegramAt = new Map(); // `${scanner}:${stage}` -> epoch ms of last TIMEOUT alert
// Restart-safe. A bare Map re-armed the 30-minute window on every restart,
// which is how duplicate TIMEOUT Telegrams fired. Seeded from db.data on
// first read and written back on every send. The watchdog's own throttle is
// already persistent (position-watchdog.js alertOnce) and is not touched.
function throttleStore() {
  db.data.scanner_config = db.data.scanner_config || {};
  db.data.scanner_config.alert_throttle_ms = db.data.scanner_config.alert_throttle_ms || {};
  return db.data.scanner_config.alert_throttle_ms;
}
function throttleLastAt(key) {
  if (scannerErrorTelegramAt.has(key)) return scannerErrorTelegramAt.get(key);
  const persisted = Number(throttleStore()[key] || 0) || 0;
  scannerErrorTelegramAt.set(key, persisted);
  return persisted;
}
function throttleMark(key, at) {
  scannerErrorTelegramAt.set(key, at);
  const store = throttleStore();
  store[key] = at;
  // Keep it bounded: drop anything older than a day. Without this the map
  // grows one key per scanner+stage pair seen, forever.
  const cutoff = at - 24 * 60 * 60_000;
  for (const k of Object.keys(store)) if (Number(store[k] || 0) < cutoff) delete store[k];
}
const SCANNER_ERROR_CLASSES = new Set(['TIMEOUT', 'HTTP_ERROR', 'PARSE', 'OTHER']);
const SCANNER_ERROR_RATE_LIMIT = 20; // per scanner per minute -- real failures, not high-frequency telemetry
let riskGateTail = Promise.resolve();
async function serializeRiskGate(req,res,next){
  let release; const mine=new Promise(resolve=>{release=resolve;}); const prior=riskGateTail; riskGateTail=mine;
  await prior; let done=false; const unlock=()=>{if(!done){done=true;release();}};
  res.once('finish',unlock); res.once('close',unlock); next();
}
const CONFIG_PUBLIC_KEYS = ['account_size','max_global_heat_pct','max_open_positions','kill_switch','paper_only','gemini_model','updated_at'];
const CONFIG_WRITE_KEYS = new Set(['account_size','max_global_heat_pct','max_open_positions','kill_switch','paper_only','gemini_model']);

const SCANNERS = ['fmp', 'forex', 'comm', 'pa', 'vp', 'fb', 'main', 'all'];

// ── Auth middleware ───────────────────────────────────────────
function auth(req, res, next) {
  const bearer = String(req.headers.authorization || '').replace(/^Bearer /i, '');
  const token = req.headers['x-token'] || req.query.token || bearer;
  if (!token) return res.status(401).json({ error: 'No token' });
  const s = (db.data.sessions || []).find(s => s.token === token && s.expires > Date.now());
  if (!s) return res.status(401).json({ error: 'Session expired' });
  req.investorId = s.investor_id;
  req.isAdmin    = s.is_admin || false;
  next();
}
function adminOnly(req, res, next) {
  auth(req, res, () => { if (!req.isAdmin) return res.status(403).json({ error: 'Admin only' }); next(); });
}
function scannerAuth(req, res, next) {
  if (!SCANNER_API_KEY) return next();
  const bearer = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
  const supplied = String(req.headers['x-scanner-key'] || bearer);
  const expected = Buffer.from(SCANNER_API_KEY);
  const actual = Buffer.from(supplied);
  if (actual.length !== expected.length || !crypto.timingSafeEqual(actual, expected)) {
    return res.status(401).json({ error: 'Invalid scanner key' });
  }
  next();
}
function serviceOrAdmin(req, res, next) {
  const hasServiceCredential = req.headers['x-scanner-key'] || req.headers.authorization;
  if (SCANNER_API_KEY && hasServiceCredential) return scannerAuth(req, res, next);
  return adminOnly(req, res, next);
}
function loginRateLimit(req, res, next) {
  const key = req.ip || req.socket.remoteAddress || 'unknown';
  const nowMs = Date.now();
  const state = loginAttempts.get(key) || { count: 0, resetAt: nowMs + 15 * 60 * 1000 };
  if (nowMs > state.resetAt) {
    state.count = 0;
    state.resetAt = nowMs + 15 * 60 * 1000;
  }
  state.count++;
  loginAttempts.set(key, state);
  if (state.count > 20) {
    return res.status(429).json({ error: 'Too many login attempts. Try again later.' });
  }
  next();
}

// ════════════════════════════════════════════════════════
// ANALYTICS ENGINE
// ════════════════════════════════════════════════════════

function calcFundStats() {
  const trades  = db.data.trades || [];
  const closed  = trades.filter(t => t.status === 'CLOSED' && t.pnl != null);
  const open    = trades.filter(t => ['OPEN','PARTIAL'].includes(t.status));
  const wins    = closed.filter(t => t.pnl > 0);
  const losses  = closed.filter(t => t.pnl <= 0);
  const totalPnl   = closed.reduce((s, t) => s + t.pnl, 0);
  const grossWin   = wins.reduce((s, t) => s + t.pnl, 0);
  const grossLoss  = Math.abs(losses.reduce((s, t) => s + t.pnl, 0));
  const avgWin     = wins.length  ? grossWin  / wins.length  : 0;
  const avgLoss    = losses.length ? grossLoss / losses.length : 0;
  const profitFactor = grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0;
  const winRate    = closed.length ? wins.length / closed.length * 100 : 0;

  // Max drawdown from running equity
  let peak = 0, maxDD = 0, runEq = 0;
  closed.sort((a,b) => a.closed_at?.localeCompare(b.closed_at)).forEach(t => {
    runEq += t.pnl;
    if (runEq > peak) peak = runEq;
    const dd = peak - runEq;
    if (dd > maxDD) maxDD = dd;
  });

  // Per scanner
  const scannerStats = {};
  SCANNERS.filter(s=>s!=='all').forEach(sc => {
    const st = closed.filter(t => t.scanner === sc);
    const sw = st.filter(t => t.pnl > 0);
    scannerStats[sc] = {
      trades: st.length, wins: sw.length,
      pnl: parseFloat(st.reduce((s,t)=>s+t.pnl,0).toFixed(2)),
      win_rate: st.length ? parseFloat((sw.length/st.length*100).toFixed(1)) : 0
    };
  });

  // Monthly returns
  const monthly = {};
  closed.forEach(t => {
    if (!t.closed_at) return;
    const mo = t.closed_at.substring(0,7);
    if (!monthly[mo]) monthly[mo] = 0;
    monthly[mo] += t.pnl;
  });

  return {
    total_pnl:     parseFloat(totalPnl.toFixed(2)),
    open_count:    open.length,
    closed_count:  closed.length,
    win_rate:      parseFloat(winRate.toFixed(1)),
    profit_factor: parseFloat(profitFactor.toFixed(2)),
    avg_win:       parseFloat(avgWin.toFixed(2)),
    avg_loss:      parseFloat(avgLoss.toFixed(2)),
    max_drawdown:  parseFloat(maxDD.toFixed(2)),
    gross_win:     parseFloat(grossWin.toFixed(2)),
    gross_loss:    parseFloat(grossLoss.toFixed(2)),
    scanner_stats: scannerStats,
    monthly_returns: Object.entries(monthly)
      .map(([month, pnl]) => ({ month, pnl: parseFloat(pnl.toFixed(2)) }))
      .sort((a,b) => a.month.localeCompare(b.month))
  };
}

function calcInvestorBalance(inv) {
  const myStakes = (db.data.stakes||[]).filter(s => s.investor_id === inv.id);
  const totalStaked = myStakes.reduce((s,x) => s+x.amount, 0);
  const alloc = (db.data.allocations||[]).filter(a => a.investor_id === inv.id);
  const risk  = (db.data.risk_settings||[]).find(r => r.investor_id === inv.id);

  // P&L per stake proportionally
  let totalPnl = 0;
  const stakeDetails = myStakes.map(stake => {
    const scopeStakes = (db.data.stakes||[]).filter(s => s.scope===stake.scope && s.ts<=stake.ts);
    const scopeTotal  = scopeStakes.reduce((s,x)=>s+x.amount, 0);
    const myShare     = scopeTotal > 0 ? stake.amount / scopeTotal : 0;
    const scopeTrades = (db.data.trades||[]).filter(t =>
      t.status==='CLOSED' && t.pnl!=null && t.ts>=stake.ts &&
      (stake.scope==='all' || t.scanner===stake.scope)
    );
    const scopePnl = scopeTrades.reduce((s,t)=>s+t.pnl, 0);
    const myPnl    = myShare * scopePnl;
    totalPnl += myPnl;
    return { ...stake, my_share_pct: parseFloat((myShare*100).toFixed(2)), scope_pnl: parseFloat(scopePnl.toFixed(2)), my_pnl: parseFloat(myPnl.toFixed(2)) };
  });

  const feePct     = (db.data.fund.performance_fee_pct||0) / 100;
  const grossPnl   = totalPnl;
  const perfFee    = totalPnl > 0 ? totalPnl * feePct : 0;
  const netPnl     = totalPnl - perfFee;
  const withdrawn  = (db.data.withdrawals||[]).filter(w=>w.investor_id===inv.id&&w.status==='PAID').reduce((s,w)=>s+w.amount,0);
  const balance    = totalStaked + netPnl - withdrawn;

  // Per-month for this investor
  const monthly = {};
  stakeDetails.forEach(stake => {
    const scopeTrades = (db.data.trades||[]).filter(t=>t.status==='CLOSED'&&t.pnl!=null&&t.ts>=stake.ts&&(stake.scope==='all'||t.scanner===stake.scope));
    const scopeTotal  = (db.data.stakes||[]).filter(s=>s.scope===stake.scope&&s.ts<=stake.ts).reduce((s,x)=>s+x.amount,0);
    const myShare     = scopeTotal>0 ? stake.amount/scopeTotal : 0;
    scopeTrades.forEach(t => {
      const mo = (t.closed_at||t.ts||'').substring(0,7);
      if (!mo) return;
      if (!monthly[mo]) monthly[mo] = 0;
      monthly[mo] += myShare * t.pnl;
    });
  });

  // Drawdown
  let peak=0, maxDD=0, runEq=0;
  Object.entries(monthly).sort((a,b)=>a[0].localeCompare(b[0])).forEach(([,pnl])=>{
    runEq+=pnl;
    if(runEq>peak) peak=runEq;
    const dd=peak-runEq; if(dd>maxDD) maxDD=dd;
  });

  return {
    ...inv,
    total_staked:   parseFloat(totalStaked.toFixed(2)),
    gross_pnl:      parseFloat(grossPnl.toFixed(2)),
    perf_fee:       parseFloat(perfFee.toFixed(2)),
    net_pnl:        parseFloat(netPnl.toFixed(2)),
    withdrawn:      parseFloat(withdrawn.toFixed(2)),
    current_balance:parseFloat(balance.toFixed(2)),
    roi_pct:        totalStaked>0 ? parseFloat((netPnl/totalStaked*100).toFixed(2)) : 0,
    max_drawdown:   parseFloat(maxDD.toFixed(2)),
    stakes:         stakeDetails,
    allocations:    alloc,
    risk_settings:  risk || null,
    monthly_returns: Object.entries(monthly).map(([month,pnl])=>({month,pnl:parseFloat(pnl.toFixed(2))})).sort((a,b)=>a.month.localeCompare(b.month))
  };
}

// ════════════════════════════════════════════════════════
// AUTH ENDPOINTS
// ════════════════════════════════════════════════════════

app.post('/api/auth/admin', loginRateLimit, async (req,res)=>{
  const { pin } = req.body;
  const adminPin = process.env.ADMIN_PIN;
  if (hashPin(pin) !== hashPin(adminPin)) return res.status(401).json({ error:'Wrong PIN' });
  const token = mkToken();
  db.data.sessions = (db.data.sessions||[]).filter(s=>s.expires>Date.now());
  db.data.sessions.push({ token, investor_id:'admin', is_admin:true, expires:Date.now()+SESSION_TTL, ts:now() });
  await save(); res.json({ token, role:'admin', name:'Admin' });
});

app.post('/api/auth/investor', loginRateLimit, async (req,res)=>{
  const { name, pin } = req.body;
  const inv = (db.data.investors||[]).find(i=>i.name.toLowerCase()===name?.toLowerCase()&&i.pin_hash===hashPin(pin)&&i.active);
  if (!inv) return res.status(401).json({ error:'Invalid name or PIN' });
  const token = mkToken();
  db.data.sessions.push({ token, investor_id:inv.id, is_admin:false, expires:Date.now()+SESSION_TTL, ts:now() });
  db.data.sessions = db.data.sessions.filter(s=>s.expires>Date.now());
  await save(); res.json({ token, role:'investor', name:inv.name });
});

app.post('/api/auth/logout', auth, async (req,res)=>{
  const token = req.headers['x-token'];
  db.data.sessions = db.data.sessions.filter(s=>s.token!==token);
  await save(); res.json({ status:'ok' });
});

app.get('/api/auth/validate', auth, (req, res) => {
  res.json({ ok: true, isAdmin: req.isAdmin });
});

// ════════════════════════════════════════════════════════
// FUND CONFIG
// ════════════════════════════════════════════════════════

app.get('/api/fund', adminOnly, (req,res) => res.json(db.data.fund));

app.patch('/api/fund', adminOnly, async (req,res)=>{
  Object.assign(db.data.fund, req.body);
  await save(); res.json({ status:'ok', fund:db.data.fund });
});

// ════════════════════════════════════════════════════════
// INVESTOR MANAGEMENT
// ════════════════════════════════════════════════════════

app.get('/api/investors', adminOnly, (req,res)=>{
  const investors = (db.data.investors||[]).map(inv => calcInvestorBalance(inv));
  const totalAUM  = investors.filter(i=>i.active).reduce((s,i)=>s+i.current_balance,0);
  const totalStaked = investors.filter(i=>i.active).reduce((s,i)=>s+i.total_staked,0);
  res.json({ investors, totalAUM:parseFloat(totalAUM.toFixed(2)), totalStaked:parseFloat(totalStaked.toFixed(2)) });
});

app.post('/api/investors', adminOnly, async (req,res)=>{
  const { name, pin, email, phone, notes } = req.body;
  if (!name||!pin) return res.status(400).json({ error:'name and pin required' });
  if ((db.data.investors||[]).find(i=>i.name.toLowerCase()===name.toLowerCase()))
    return res.status(400).json({ error:'Investor name already exists' });
  const inv = {
    id: 'inv_'+Date.now(), name, email:email||'', phone:phone||'', notes:notes||'',
    pin_hash:hashPin(pin), active:true, suspended:false,
    joined_at:now(), high_watermark:0
  };
  db.data.investors.push(inv);
  // Default risk settings
  db.data.risk_settings.push({
    id: nid('risk_settings'), investor_id:inv.id,
    max_risk_pct_per_trade:2, max_open_positions:5,
    max_daily_loss_pct:5, max_monthly_loss_pct:15,
    scanners_enabled:['fmp','forex','comm','pa','vp','fb','main'],
    position_size_override:null, updated_at:now()
  });
  await save(); res.json({ status:'ok', investor:{id:inv.id,name,email} });
});

app.patch('/api/investors/:id', adminOnly, async (req,res)=>{
  const inv = (db.data.investors||[]).find(i=>i.id===req.params.id);
  if (!inv) return res.status(404).json({ error:'Not found' });
  const { name,email,phone,notes,pin,active,suspended } = req.body;
  if (name)  inv.name  = name;
  if (email) inv.email = email;
  if (phone) inv.phone = phone;
  if (notes) inv.notes = notes;
  if (pin)   inv.pin_hash = hashPin(pin);
  if (active!==undefined)    inv.active    = active;
  if (suspended!==undefined) inv.suspended = suspended;
  await save(); res.json({ status:'ok' });
});

app.get('/api/investors/:id', adminOnly, (req,res)=>{
  const inv = (db.data.investors||[]).find(i=>i.id===req.params.id);
  if (!inv) return res.status(404).json({ error:'Not found' });
  res.json(calcInvestorBalance(inv));
});

// ════════════════════════════════════════════════════════
// STAKES
// ════════════════════════════════════════════════════════

app.get('/api/stakes', adminOnly, (req,res)=>{
  const stakes = (db.data.stakes||[]).map(s=>{
    const inv = (db.data.investors||[]).find(i=>i.id===s.investor_id);
    return { ...s, investor_name: inv?.name||'Unknown' };
  });
  res.json(stakes);
});

app.post('/api/stakes', adminOnly, async (req,res)=>{
  const { investor_id, amount, scope, payment_method, notes } = req.body;
  if (!investor_id||!amount||!scope) return res.status(400).json({ error:'investor_id, amount, scope required' });
  if (!SCANNERS.includes(scope)) return res.status(400).json({ error:'Invalid scope' });
  const inv = (db.data.investors||[]).find(i=>i.id===investor_id);
  if (!inv) return res.status(404).json({ error:'Investor not found' });
  const stake = { id:nid('stakes'), investor_id, amount:parseFloat(amount), scope, payment_method:payment_method||'bank_transfer', notes:notes||'', ts:now() };
  db.data.stakes.push(stake);
  await save(); res.json({ status:'ok', stake });
});

app.delete('/api/stakes/:id', adminOnly, async (req,res)=>{
  const id = parseInt(req.params.id);
  db.data.stakes = (db.data.stakes||[]).filter(s=>s.id!==id);
  await save(); res.json({ status:'ok' });
});

// ════════════════════════════════════════════════════════
// ALLOCATIONS
// ════════════════════════════════════════════════════════

app.get('/api/allocations/:investor_id', adminOnly, (req,res)=>{
  res.json((db.data.allocations||[]).filter(a=>a.investor_id===req.params.investor_id));
});

app.post('/api/allocations', adminOnly, async (req,res)=>{
  const { investor_id, scanner, allocation_pct, amount } = req.body;
  // Upsert
  const existing = (db.data.allocations||[]).find(a=>a.investor_id===investor_id&&a.scanner===scanner);
  if (existing) {
    existing.allocation_pct = allocation_pct;
    existing.amount = amount;
    existing.updated_at = now();
  } else {
    db.data.allocations.push({ id:nid('allocations'), investor_id, scanner, allocation_pct:parseFloat(allocation_pct||0), amount:parseFloat(amount||0), updated_at:now() });
  }
  await save(); res.json({ status:'ok' });
});

// ════════════════════════════════════════════════════════
// RISK SETTINGS
// ════════════════════════════════════════════════════════

app.get('/api/risk/:investor_id', adminOnly, (req,res)=>{
  const r = (db.data.risk_settings||[]).find(r=>r.investor_id===req.params.investor_id);
  res.json(r || {});
});

app.patch('/api/risk/:investor_id', adminOnly, async (req,res)=>{
  const r = (db.data.risk_settings||[]).find(r=>r.investor_id===req.params.investor_id);
  if (!r) return res.status(404).json({ error:'Risk settings not found' });
  Object.assign(r, req.body, { updated_at:now() });
  await save(); res.json({ status:'ok', risk:r });
});

// ════════════════════════════════════════════════════════
// WITHDRAWALS & FEES
// ════════════════════════════════════════════════════════

app.get('/api/withdrawals', adminOnly, (req,res)=>{
  const wdls = (db.data.withdrawals||[]).map(w=>{
    const inv = (db.data.investors||[]).find(i=>i.id===w.investor_id);
    return { ...w, investor_name:inv?.name||'Unknown' };
  });
  res.json(wdls);
});

app.post('/api/withdrawals', adminOnly, async (req,res)=>{
  const { investor_id, amount, type, notes } = req.body;
  if (!investor_id||!amount) return res.status(400).json({ error:'investor_id and amount required' });
  const w = { id:'wdl_'+Date.now(), investor_id, amount:parseFloat(amount), type:type||'withdrawal', status:'REQUESTED', notes:notes||'', requested_at:now(), paid_at:null };
  db.data.withdrawals.push(w);
  await save(); res.json({ status:'ok', withdrawal:w });
});

app.patch('/api/withdrawals/:id', adminOnly, async (req,res)=>{
  const w = (db.data.withdrawals||[]).find(x=>x.id===req.params.id);
  if (!w) return res.status(404).json({ error:'Not found' });
  w.status='PAID'; w.paid_at=now();
  if(req.body.notes) w.notes=req.body.notes;
  await save(); res.json({ status:'ok' });
});

app.post('/api/withdrawals/:id/cancel', adminOnly, async (req,res)=>{
  const w = (db.data.withdrawals||[]).find(x=>x.id===req.params.id);
  if (!w) return res.status(404).json({ error:'Not found' });
  w.status='CANCELLED';
  await save(); res.json({ status:'ok' });
});

// Investor withdrawal request
app.post('/api/investor/withdraw', auth, async (req,res)=>{
  if (req.isAdmin) return res.status(403).json({ error:'Use admin endpoint' });
  const pending = (db.data.withdrawals||[]).find(w=>w.investor_id===req.investorId&&w.status==='REQUESTED');
  if (pending) return res.status(400).json({ error:'You already have a pending request' });
  const { amount, notes } = req.body;
  const w = { id:'wdl_'+Date.now(), investor_id:req.investorId, amount:parseFloat(amount), type:'withdrawal', status:'REQUESTED', notes:notes||'', requested_at:now(), paid_at:null };
  db.data.withdrawals.push(w);
  await save(); res.json({ status:'ok' });
});

// ════════════════════════════════════════════════════════
// SCANNER WEBHOOKS (from n8n)
// ════════════════════════════════════════════════════════

// ── Input validation helpers ─────────────────────────────────
function validTicker(t) { return typeof t === 'string' && t.length >= 1 && t.length <= 20 && /^[A-Za-z0-9\.\-_]+$/.test(t); }
function validDirection(d) { return ['BUY','SELL','LONG','SHORT'].includes(String(d).toUpperCase()); }
function validNumber(n, min, max) { const v = Number(n); return isFinite(v) && v >= min && v <= max; }

app.post('/api/signal', scannerAuth, async (req,res)=>{
  try {
    const b=req.body;
    if (!validTicker(b.ticker||b.pair||b.asset)) return res.status(400).json({status:'error',message:'valid ticker required (1-20 chars, alphanumeric)'});
    if (b.entry != null && !validNumber(b.entry, 0.0001, 1000000)) return res.status(400).json({status:'error',message:'entry must be 0.0001-1000000'});
    const scanner = b.scanner||'unknown';
    const signal = { id:nid('signals'), ts:b.ts||now(), scanner, type:b.type||'skip', ticker:(b.ticker||b.pair||b.asset).toUpperCase(), detail:b.detail||'', entry:b.entry||b.entry_price||null, sl:b.sl||b.stop_loss||null, tp:b.tp||b.take_profit_1||null, quality:b.quality_score||null, adx:b.adx||null, rsi:b.rsi||null, volume_ratio:b.volume_ratio||null, config_hash:await getConfigHash(scanner, db.data.scanner_config||{}) };
    db.data.signals.unshift(signal);
    if(db.data.signals.length>1000) db.data.signals=db.data.signals.slice(0,1000);
    await saveSignalHot(signal); res.json({status:'ok'});
    await journalEvent('signal', signal).catch(()=>{});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/trade/open', scannerAuth, async (req,res)=>{
  try {
    const b=req.body;
    // Validation
    if (!validTicker(b.ticker||b.epic)) return res.status(400).json({status:'error',message:'valid ticker required (1-20 chars, alphanumeric)'});
    if (!validDirection(b.direction)) return res.status(400).json({status:'error',message:'direction must be BUY, SELL, LONG, or SHORT'});
    if (b.entry != null && !validNumber(b.entry, 0.0001, 1000000)) return res.status(400).json({status:'error',message:'entry must be 0.0001-1000000'});
    if (b.sl != null && !validNumber(b.sl, 0.0001, 1000000)) return res.status(400).json({status:'error',message:'sl must be 0.0001-1000000'});
    if (b.size != null && !validNumber(b.size, 1, 1000000)) return res.status(400).json({status:'error',message:'size must be 1-1000000'});
    if (b.risk_usd != null && !validNumber(b.risk_usd, 0.01, 100000)) return res.status(400).json({status:'error',message:'risk_usd must be 0.01-100000'});
    const dealId = String(b.deal_id || b.dealId || '').trim();
    if (!dealId || ['UNKNOWN','FAILED'].includes(dealId.toUpperCase())) return res.status(400).json({status:'error',reason:'NO_BROKER_CONFIRMATION',message:'valid confirmed broker deal_id required'});
    if (dealId) {
      const existing = (db.data.trades || []).find(t => t.deal_id === dealId && ['OPEN','PARTIAL'].includes(t.status));
      if (existing) {
        const attempted = { setup_type:b.setup_type||b.type||'', intended_entry:b.intended_entry??b.intendedEntry??null, initial_sl:b.sl??b.stop_loss??null, scanner:b.scanner||'unknown', opened_at:b.ts||null };
        for (const [field,value] of Object.entries(attempted)) {
          if (value != null && String(existing[field] ?? '') !== String(value)) {
            const violation = await recordStampViolation({ trade:existing, field, attemptedValue:value, payload:b });
            if (violation?.alert_needed) {
              await sendTelegramAlert(`WRITE-ONCE STAMP VIOLATION: ${existing.ticker} ${field} mutation rejected.`).catch(()=>{});
            }
            return res.status(409).json({status:'error',reason:'WRITE_ONCE_STAMP',field});
          }
        }
        return res.json({status:'ok', duplicate:true, trade_id:existing.id});
      }
    }
    const entry = b.entry ?? b.entry_price ?? null;
    const intendedEntry = b.intended_entry ?? b.intendedEntry ?? null;
    const initialSl = b.sl ?? b.stop_loss ?? null;
    const scanner = b.scanner||'unknown';
    const size=Number(b.size??0),bid=Number(b.bid),ask=Number(b.ask);
    const spreadCost=b.spread_cost==null?(Number.isFinite(bid)&&Number.isFinite(ask)&&size>0?Math.max(0,(ask-bid)*size):null):Number(b.spread_cost);
    const signalTime=new Date(b.signal_ts||b.signal_time||b.ts||Date.now()),orderTime=b.order_ts||b.order_time||b.order_placed_at?new Date(b.order_ts||b.order_time||b.order_placed_at):null,fillTime=new Date(b.fill_ts||b.fill_time||b.filled_at||Date.now());
    const signalToOrderMs=orderTime&&Number.isFinite(signalTime.getTime())&&Number.isFinite(orderTime.getTime())?Math.max(0,orderTime-signalTime):null;
    const orderToFillMs=orderTime&&Number.isFinite(fillTime.getTime())&&Number.isFinite(orderTime.getTime())?Math.max(0,fillTime-orderTime):null;
    const trade = { id:nid('trades'), ts:b.ts||now(), scanner, ticker:(b.ticker||b.epic).toUpperCase(), deal_id:dealId, direction:String(b.direction).toUpperCase(), setup_type:b.setup_type||b.type||'',
      // NEW field, never folded into setup_type: that is a matching key with
      // exact-equality semantics in the Trade Brain. Null until a scanner sends it.
      engine_branch:b.engine_branch||b.branch||null, entry, intended_entry:intendedEntry, fill_slippage_pct:computeFillSlippage(b.direction, entry, intendedEntry), sl:initialSl, initial_sl:initialSl, tp1:b.tp1||b.take_profit_1||null, tp2:b.tp2||b.take_profit_2||null, size:b.size||null, risk_usd:b.risk_usd||null, spread_cost:spreadCost, commission:Number(b.commission??0), financing_accrued:0, pnl_gross:null, pnl_net:null, signal_to_order_ms:signalToOrderMs, order_to_fill_ms:orderToFillMs, bracket_mode:b.bracket_mode||null, fill_drift_pct:b.fill_drift_pct==null?null:Number(b.fill_drift_pct), measurement_population:b.measurement_population||'ENTERED', config_hash:await getConfigHash(scanner, db.data.scanner_config||{}), status:'OPEN', close_price:null, pnl:null, max_favorable:entry, max_adverse:entry, mae_r:null, mfe_r:null, opened_at:b.fill_ts||b.fill_time||b.filled_at||b.ts||now(), closed_at:null };
    db.data.trades.unshift(trade);
    await saveTradeHot(trade);
    if (orderTime) await journalEvent('order_placed', trade).catch(()=>{});
    await journalEvent('trade_open', trade).catch(()=>{});
    res.json({status:'ok'});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/trade/close', scannerAuth, async (req,res)=>{
  try {
    const b=req.body;
    const t=findTradeForMutation(db.data.trades,b);
    if(t){
      let closePrice = b.close_price??b.closePrice??b.current_price??null;
      let grossIn    = b.pnl_gross??b.pnl??b.pnl_realised??null;

      // An economics-free close is refused, not recorded. If the caller
      // supplied neither a price nor a pnl, ask Capital; if Capital
      // cannot answer either, 422 and leave the trade OPEN for retry.
      // Recording CLOSED with nulls loses the money permanently.
      if (closePrice == null && grossIn == null) {
        const resolved = await resolveCloseEconomics({
          deal_id: t.deal_id, epic: t.ticker, direction: t.direction,
          size: t.size ?? b.size, opened_at: t.opened_at
        }, { login: capitalLogin, invalidate: invalidateCapitalSession }).catch(e => ({ ok:false, error:e.message }));

        if (!resolved.ok) {
          await journalEvent('scanner_error', {
            scanner: b.scanner || t.scanner || null, ticker: t.ticker || null,
            payload: { scanner: b.scanner || t.scanner || null, stage: 'trade_close_economics',
                       error_class: 'OTHER', reason: 'CLOSE_ECONOMICS_UNRESOLVED',
                       message: resolved.error || 'unresolved', deal_id: t.deal_id,
                       occurred_at: now() }
          }).catch(()=>{});
          return res.status(422).json({
            status:'error', reason:'CLOSE_ECONOMICS_UNRESOLVED',
            message:'close carried no price and no pnl, and Capital could not resolve them; trade left OPEN for retry',
            trade_id: t.id, deal_id: t.deal_id, detail: resolved.error || null
          });
        }
        closePrice = resolved.close_price;
        t.close_source = resolved.close_source;
        t.close_resolved_from = 'capital /history/activity detailed=true';
        const dir = String(t.direction||'').toUpperCase();
        const isLong = dir === 'BUY' || dir === 'LONG';
        const sz = Number(t.size ?? b.size);
        const entry = Number(t.entry);
        if (Number.isFinite(sz) && Number.isFinite(entry)) {
          grossIn = +(((isLong ? closePrice - entry : entry - closePrice)) * sz).toFixed(2);
        }
      }
      applyTradePricePath(t, closePrice, onPriceTickRejected('trade/close'));
      t.status=b.action==='PARTIAL_EXIT'?'PARTIAL':'CLOSED';
      t.close_price=closePrice;
      t.pnl_gross=grossIn;
      t.commission=Number(t.commission||0)+Number(b.commission||0);
      t.pnl_net=t.pnl_gross==null?null:computeNetPnl(t);
      t.pnl=t.pnl_net;
      if(t.status==='CLOSED') {
        t.closed_at=b.ts||now();
        finalizeTradePath(t);
      }
    }
    const update = { id:nid('updates'), ts:b.ts||now(), deal_id:b.deal_id||null, ticker:b.ticker||null, scanner:b.scanner||null, action:b.action||'FULL_EXIT', close_price:b.close_price||null, pnl_realised:b.pnl||null };
    db.data.updates.unshift(update);

    // ── Auto-feed the Trade Brain on a FULL close ──
    if (t && t.status === 'CLOSED') {
      const pnl = Number(t.pnl ?? b.pnl ?? 0);
      const risk = Number(t.risk_amount ?? t.risk_usd ?? 0);
      const src = { ...t, ...b, pnl };
      const rMultiple = risk > 0 ? +(pnl / risk).toFixed(2) : null;
      const rec = {
        id: 'brain_' + Date.now() + '_' + Math.random().toString(36).slice(2,7),
        scanner: (t.scanner||'').toLowerCase(),
        setup_type: (t.setup_type||t.trade_type||t.strategy||'').toUpperCase(),
        direction: (t.direction||'').toUpperCase(),
        ticker: t.ticker||t.symbol||t.pair||t.epic||'',
        features: brainFeatures(src),
        outcome: { win: pnl>0, r_multiple: rMultiple, pnl: +pnl.toFixed(2) },
        deal_id: t.deal_id||b.deal_id||'',
        recorded_at: now()
      };
      db.data.trade_brain = db.data.trade_brain || [];
      if (rec.deal_id) db.data.trade_brain = db.data.trade_brain.filter(r=>r.deal_id!==rec.deal_id);
      db.data.trade_brain.push(rec);
      await saveBrainHot(rec);
    }

    if (t) await saveTradeHot(t);
    if (t?.status === 'CLOSED') await journalEvent(b.external_close ? 'external_close' : 'trade_close', t).catch(()=>{});
    await saveUpdateHot(update);
    res.json({status:'ok',found:!!t});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/trade/update', scannerAuth, async (req,res)=>{
  try {
    const b=req.body;
    const t=findTradeForMutation(db.data.trades,b);
    if(t&&b.new_sl) t.sl=b.new_sl;
    if(t) applyTradePricePath(t, b.current_price, onPriceTickRejected('trade/update'));
    const update = { id:nid('updates'), ts:b.ts||now(), deal_id:b.deal_id||null, ticker:b.ticker||null, scanner:b.scanner||null, action:'UPDATE_SL', old_sl:b.old_sl||null, new_sl:b.new_sl||null, current_price:b.current_price??null };
    db.data.updates.unshift(update);
    if (t) await saveTradeHot(t);
    await saveUpdateHot(update);
    res.json({status:'ok',found:!!t});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/rejection', scannerAuth, async (req,res)=>{
  try {
    const b=req.body||{};
    const scanner=String(b.scanner||'').trim().toLowerCase(), ticker=String(b.ticker||'').trim().toUpperCase();
    if(!SCANNER_ORDER.includes(scanner)) return res.status(400).json({error:'valid scanner required'});
    const minute=Math.floor(Date.now()/60000), rateKey=`${scanner}:${minute}`, count=(rejectionRates.get(rateKey)||0)+1;
    rejectionRates.set(rateKey,count); for(const k of rejectionRates.keys())if(!k.endsWith(`:${minute}`))rejectionRates.delete(k);
    if(count>120) return res.status(429).json({error:'rejection rate limit exceeded'});
    if(!/^[A-Z0-9.^_-]{1,30}$/.test(ticker)||ticker==='UNKNOWN') return res.status(400).json({error:'valid ticker required'});
    const rejection = {
      id:nid('rejections'),
      ts:b.ts||now(),
      scanner,ticker,
      reason:b.reason||'UNKNOWN',
      detail:b.detail||b.message||'',
      direction:b.direction||'',
      intended_entry:b.intended_entry??null,
      intended_sl:b.intended_sl??null,
      intended_tp1:b.intended_tp1??null,
      intended_tp2:b.intended_tp2??null,
      rejected_price:b.rejected_price??null,
      rejected_time:b.rejected_time||b.ts||now(),
      risk_response:b.risk_response??null,
      item_keys:b.item_keys??null,
      score:b.score??null
      ,measurement_population:'NEVER_ENTERED', config_hash:b.config_hash??null,
      source_type:b.source_type??'rejection', setup_type:b.setup_type??null,
      signal_ts:b.signal_ts??null, order_ts:b.order_ts??null
    };
    db.data.rejections.unshift(rejection);
    if(db.data.rejections.length>500) db.data.rejections=db.data.rejections.slice(0,500);
    await saveRejectionHot(rejection); res.json({status:'ok'});
    await journalEvent('rejection', rejection).catch(()=>{});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

// Scanner-side failure reporter. The 44% of production risk/close calls that
// exceeded the scanner timeout for two months produced zero rows anywhere --
// this is the receiver so the next silent scanner-side failure is not
// equally invisible. Deliberately narrow: named fields only, no `...b`
// spread (the /api/rejection defect), unknown scanner rejected outright.
app.post('/api/scanner/error', scannerAuth, async (req, res) => {
  try {
    const b = req.body || {};
    const scanner = String(b.scanner || '').trim().toLowerCase();
    if (!SCANNER_ORDER.includes(scanner)) return res.status(400).json({ error: 'valid scanner required' });

    const minute = Math.floor(Date.now() / 60000);
    const rateKey = `${scanner}:${minute}`;
    const entry = scannerErrorRates.get(rateKey) || { count: 0, dropped: 0 };
    entry.count++;
    for (const k of scannerErrorRates.keys()) if (!k.endsWith(`:${minute}`)) scannerErrorRates.delete(k);
    if (entry.count > SCANNER_ERROR_RATE_LIMIT) {
      entry.dropped++;
      scannerErrorRates.set(rateKey, entry);
      console.warn(`[scanner-error] rate limit hit for ${scanner}: ${entry.dropped} dropped this minute`);
      return res.status(202).json({ status: 'dropped', reason: 'rate_limited', dropped: entry.dropped });
    }
    scannerErrorRates.set(rateKey, entry);

    const stage = String(b.stage || '').trim().slice(0, 60) || 'unknown';
    const errorClass = SCANNER_ERROR_CLASSES.has(String(b.error_class || '').toUpperCase())
      ? String(b.error_class).toUpperCase() : 'OTHER';
    const httpStatus = Number.isFinite(Number(b.http_status)) ? Number(b.http_status) : null;
    const durationMs = Number.isFinite(Number(b.duration_ms)) ? Number(b.duration_ms) : null;
    const dealId = b.deal_id != null ? String(b.deal_id).slice(0, 80) : null;
    const ticker = b.ticker != null ? String(b.ticker).trim().toUpperCase().slice(0, 20) : null;
    const message = b.message != null ? String(b.message).slice(0, 500) : '';
    const occurredAt = b.occurred_at || now();

    const event = await journalEvent('scanner_error', {
      scanner, ticker,
      payload: { scanner, stage, error_class: errorClass, http_status: httpStatus,
        duration_ms: durationMs, deal_id: dealId, ticker, message, occurred_at: occurredAt, received_at: now() }
    }).catch(e => { console.error('[scanner-error] journal write failed:', e.message); return null; });

    if (errorClass === 'TIMEOUT') {
      const throttleKey = `${scanner}:${stage}`;
      const lastAt = throttleLastAt(throttleKey);
      if (Date.now() - lastAt >= 30 * 60_000) {
        throttleMark(throttleKey, Date.now());
        sendTelegramAlert(
          `SCANNER ERROR: ${scanner}/${stage} TIMEOUT` +
          (durationMs != null ? ` after ${durationMs}ms` : '') +
          (dealId ? ` deal_id=${dealId}` : '') +
          (message ? ` -- ${message.slice(0, 200)}` : '')
        ).catch(() => {});
      }
    }

    res.json({ status: 'ok', recorded: !!event });
  } catch (e) { res.status(500).json({ status: 'error', message: e.message }); }
});

app.post('/api/rejection-analysis', scannerAuth, async (req,res)=>{
  try {
    const b = req.body || {};
    const date = (b.date || today()).slice(0,10);
    const scanner = (b.scanner || 'fmp').toLowerCase();
    const rows = Array.isArray(b.rows) ? b.rows : [];
    db.data.rejection_analysis = db.data.rejection_analysis || [];
    db.data.rejection_analysis = db.data.rejection_analysis.filter(r =>
      !((r.date || '').slice(0,10) === date && (r.scanner || '').toLowerCase() === scanner)
    );
    rows.forEach((row, i) => {
      const verdict = (row.verdict || 'NEUTRAL').toUpperCase();
      db.data.rejection_analysis.push({
        id: `rejq_${date}_${scanner}_${i}_${Date.now()}`,
        date,
        scanner,
        ticker: row.ticker || 'UNKNOWN',
        reason: row.reason || row.skip_reason || 'UNKNOWN',
        rejection_stage: row.rejection_stage || '',
        direction: row.direction || '',
        rejected_price: Number(row.rejected_price ?? 0),
        max_favorable_pct: Number(row.max_favorable_pct ?? row.max_fav ?? 0),
        max_adverse_pct: Number(row.max_adverse_pct ?? row.max_adv ?? 0),
        tp_sl_outcome: row.tp_sl_outcome || row.TP_SL_Outcome || 'NO_LEVELS',
        verdict,
        missed_pnl: Number(row.missed_pnl ?? 0),
        intended_entry: row.intended_entry ?? null,
        intended_sl: row.intended_sl ?? null,
        intended_tp1: row.intended_tp1 ?? null,
        intended_tp2: row.intended_tp2 ?? null,
        day_high_after: row.day_high_after ?? null,
        day_low_after: row.day_low_after ?? null,
        close_price: row.close_price ?? null,
        data: row,
        created_at: now()
      });
    });
    await save();
    res.json({ status:'ok', date, scanner, rows: rows.length });
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.get('/api/rejection-analysis', (req,res)=>{
  try {
    const scanner = String(req.query.scanner || 'fmp').toLowerCase();
    const days = Math.max(1, Math.min(365, Number(req.query.days || 30)));
    const selectedDate = String(req.query.date || today()).slice(0,10);
    const cutoff = new Date(selectedDate + 'T00:00:00.000Z');
    cutoff.setUTCDate(cutoff.getUTCDate() - (days - 1));
    const rows = (db.data.rejection_analysis || []).filter(r => {
      const d = (r.date || '').slice(0,10);
      return (r.scanner || '').toLowerCase() === scanner && d >= cutoff.toISOString().slice(0,10) && d <= selectedDate;
    });
    const todayRows = rows.filter(r => (r.date || '').slice(0,10) === selectedDate);
    const summarize = list => {
      const total = list.length;
      const good = list.filter(r => r.verdict === 'GOOD_REJECT').length;
      const missed = list.filter(r => r.verdict === 'MISSED_WINNER').length;
      const neutral = list.filter(r => r.verdict === 'NEUTRAL').length;
      const missedPnl = list.reduce((s,r)=>s + Number(r.missed_pnl || 0), 0);
      return {
        total,
        good_rejects: good,
        missed_winners: missed,
        neutral,
        pct_good: total ? Math.round((good / total) * 100) : 0,
        pct_missed: total ? Math.round((missed / total) * 100) : 0,
        total_missed_pnl: +missedPnl.toFixed(2)
      };
    };
    const reasonMap = new Map();
    todayRows.forEach(r => {
      const key = r.reason || 'UNKNOWN';
      if (!reasonMap.has(key)) reasonMap.set(key, { reason:key, count:0, missed:0, good:0, neutral:0, missed_pnl:0 });
      const g = reasonMap.get(key);
      g.count++;
      if (r.verdict === 'MISSED_WINNER') g.missed++;
      else if (r.verdict === 'GOOD_REJECT') g.good++;
      else g.neutral++;
      g.missed_pnl += Number(r.missed_pnl || 0);
    });
    const by_reason = Array.from(reasonMap.values()).map(r => ({
      ...r,
      missed_pnl:+r.missed_pnl.toFixed(2),
      verdict_pct:{
        good:r.count ? Math.round((r.good/r.count)*100) : 0,
        neutral:r.count ? Math.round((r.neutral/r.count)*100) : 0,
        missed:r.count ? Math.round((r.missed/r.count)*100) : 0
      }
    })).sort((a,b)=>b.missed-a.missed || b.missed_pnl-a.missed_pnl);
    const dayMap = new Map();
    rows.forEach(r => {
      const d = (r.date || '').slice(0,10);
      if (!dayMap.has(d)) dayMap.set(d, []);
      dayMap.get(d).push(r);
    });
    const trend_30d = Array.from(dayMap.entries()).sort(([a],[b])=>a.localeCompare(b)).map(([date, list]) => {
      const s = summarize(list);
      return { date, pct_good:s.pct_good, pct_missed:s.pct_missed, missed_pnl:s.total_missed_pnl };
    });
    res.json({
      summary_today: summarize(todayRows),
      by_reason,
      trades_today: todayRows.slice().sort((a,b)=>(b.verdict === 'MISSED_WINNER') - (a.verdict === 'MISSED_WINNER')),
      trend_30d
    });
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/ping', scannerAuth, async (req,res)=>{
  try {
    const b=req.body;
    const ping = { id:nid('pings'), ts:b.ts||now(), scanner:b.scanner||'unknown', status:b.status||'running', signals_today:b.signals_today||0, open_trades:b.open_trades||0, portfolio_heat:b.portfolio_heat||0, regime:b.regime||'', message:b.message||'' };
    db.data.pings.unshift(ping);
    if(db.data.pings.length>400) db.data.pings=db.data.pings.slice(0,400);
    await savePing(ping); res.json({status:'ok'});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

// ════════════════════════════════════════════════════════
// ADMIN OVERVIEW — single endpoint for dashboard
// ════════════════════════════════════════════════════════

app.get('/api/overview', adminOnly, (req,res)=>{
  try {
    const investors  = (db.data.investors||[]).map(i=>calcInvestorBalance(i));
    const fundStats  = calcFundStats();
    const totalAUM   = investors.filter(i=>i.active).reduce((s,i)=>s+i.current_balance,0);
    const totalIn    = investors.filter(i=>i.active).reduce((s,i)=>s+i.total_staked,0);
    const totalPnl   = fundStats.total_pnl;
    const pending    = (db.data.withdrawals||[]).filter(w=>w.status==='REQUESTED').map(w=>{ const inv=(db.data.investors||[]).find(i=>i.id===w.investor_id); return {...w,investor_name:inv?.name||'?'}; });
    const latestPings= {};
    (db.data.pings||[]).forEach(p=>{ if(!latestPings[p.scanner]||p.ts>latestPings[p.scanner].ts) latestPings[p.scanner]=p; });
    const todayTrades= (db.data.trades||[]).filter(t=>isToday(t.ts));
    const todaySignals=(db.data.signals||[]).filter(s=>isToday(s.ts));
    const todayRejs   =(db.data.rejections||[]).filter(r=>isToday(r.ts));
    // Pagination support for large datasets
    const tradeLimit    = parseInt(req.query.trade_limit)    || 200;
    const signalLimit   = parseInt(req.query.signal_limit)   || 200;
    const rejectionLimit= parseInt(req.query.rejection_limit)|| 200;
    const recentTrades     = (db.data.trades||[]).slice(-tradeLimit);
    const recentSignals    = (db.data.signals||[]).slice(-signalLimit);
    const recentRejections = (db.data.rejections||[]).slice(-rejectionLimit);
    res.json({ investors, fund:db.data.fund, fund_stats:fundStats, summary:{ total_aum:parseFloat(totalAUM.toFixed(2)), total_invested:parseFloat(totalIn.toFixed(2)), total_pnl:parseFloat(totalPnl.toFixed(2)), investor_count:investors.filter(i=>i.active).length, pending_withdrawals:pending.length, open_positions:fundStats.open_count }, pending_withdrawals:pending, latest_pings:Object.values(latestPings), today:{ trades:todayTrades.length, signals:todaySignals.length, rejections:todayRejs.length }, recent_trades:recentTrades, recent_signals:recentSignals, recent_rejections:recentRejections, total_counts:{ trades:(db.data.trades||[]).length, signals:(db.data.signals||[]).length, rejections:(db.data.rejections||[]).length } });
  } catch(e){ res.status(500).json({error:e.message}); }
});

// ════════════════════════════════════════════════════════
// INVESTOR PORTAL ENDPOINT
// ════════════════════════════════════════════════════════

app.get('/api/me', auth, (req,res)=>{
  if (req.isAdmin) return res.status(403).json({ error:'Use admin endpoints' });
  try {
    const inv=(db.data.investors||[]).find(i=>i.id===req.investorId);
    if(!inv) return res.status(404).json({error:'Not found'});
    const data=calcInvestorBalance(inv);
    const myScopes=[...new Set((data.stakes||[]).map(s=>s.scope))];
    const recentTrades=(db.data.trades||[]).filter(t=>myScopes.includes('all')||myScopes.includes(t.scanner)).slice(0,30);
    const pending=(db.data.withdrawals||[]).find(w=>w.investor_id===inv.id&&w.status==='REQUESTED');
    res.json({ investor:{id:inv.id,name:inv.name,email:inv.email,joined_at:inv.joined_at}, summary:{ total_staked:data.total_staked, gross_pnl:data.gross_pnl, perf_fee:data.perf_fee, net_pnl:data.net_pnl, withdrawn:data.withdrawn, current_balance:data.current_balance, roi_pct:data.roi_pct, max_drawdown:data.max_drawdown }, stakes:data.stakes, allocations:data.allocations, risk_settings:data.risk_settings, monthly_returns:data.monthly_returns, recent_trades:recentTrades, pending_withdrawal:pending||null });
  } catch(e){ res.status(500).json({error:e.message}); }
});


// ════════════════════════════════════════════════════════
// TRADE INTELLIGENCE ENDPOINTS
// ════════════════════════════════════════════════════════

// Full trade log with filters
app.get('/api/trades', adminOnly, (req,res)=>{
  try {
    const { scanner, status, date, ticker, limit=200 } = req.query;
    let trades = db.data.trades || [];
    if (scanner && scanner !== 'all') trades = trades.filter(t => t.scanner === scanner);
    if (status  && status  !== 'all') trades = trades.filter(t => t.status === status);
    if (ticker)  trades = trades.filter(t => t.ticker?.toUpperCase().includes(ticker.toUpperCase()));
    if (date)    trades = trades.filter(t => t.ts?.startsWith(date));
    trades = trades.slice(0, parseInt(limit));
    res.json({ trades, total: trades.length });
  } catch(e) { res.status(500).json({error:e.message}); }
});

// Full signal log with filters
app.get('/api/signals', adminOnly, (req,res)=>{
  try {
    const { scanner, type, date, ticker, limit=200 } = req.query;
    let signals = db.data.signals || [];
    if (scanner && scanner !== 'all') signals = signals.filter(s => s.scanner === scanner);
    if (type    && type    !== 'all') signals = signals.filter(s => s.type === type);
    if (ticker)  signals = signals.filter(s => s.ticker?.toUpperCase().includes(ticker.toUpperCase()));
    if (date)    signals = signals.filter(s => s.ts?.startsWith(date));
    signals = signals.slice(0, parseInt(limit));
    res.json({ signals, total: signals.length });
  } catch(e) { res.status(500).json({error:e.message}); }
});

// Full rejection log with filters
app.get('/api/rejections', adminOnly, (req,res)=>{
  try {
    const { scanner, reason, date, ticker, limit=200 } = req.query;
    let rejections = db.data.rejections || [];
    if (scanner && scanner !== 'all') rejections = rejections.filter(r => r.scanner === scanner);
    if (reason  && reason  !== 'all') rejections = rejections.filter(r => r.reason === reason);
    if (ticker)  rejections = rejections.filter(r => r.ticker?.toUpperCase().includes(ticker.toUpperCase()));
    if (date)    rejections = rejections.filter(r => r.ts?.startsWith(date));
    rejections = rejections.slice(0, parseInt(limit));
    res.json({ rejections, total: rejections.length });
  } catch(e) { res.status(500).json({error:e.message}); }
});

// Single trade detail
app.get('/api/trades/:id', adminOnly, (req,res)=>{
  const trade = (db.data.trades||[]).find(t => t.id == req.params.id);
  if (!trade) return res.status(404).json({error:'Not found'});
  // Find related signal
  const signal = (db.data.signals||[]).find(s =>
    s.ticker === trade.ticker &&
    Math.abs(new Date(s.ts) - new Date(trade.ts)) < 5 * 60 * 1000
  );
  // Find related updates
  const updates = (db.data.updates||[]).filter(u =>
    u.deal_id === trade.deal_id || u.ticker === trade.ticker
  );
  // Regime at time of trade from nearest ping
  const ping = (db.data.pings||[])
    .filter(p => p.scanner === trade.scanner && p.ts <= trade.ts)
    .sort((a,b) => b.ts.localeCompare(a.ts))[0];
  res.json({ trade, signal, updates, ping });
});

// Master activity feed — signals + rejections + trades unified
app.get('/api/activity', adminOnly, (req,res)=>{
  try {
    const { scanner, date, type, ticker, limit=300 } = req.query;
    const today = date || new Date().toISOString().split('T')[0];

    let items = [];

    // Signals
    (db.data.signals||[]).filter(s => !date || s.ts?.startsWith(today)).forEach(s => {
      items.push({ ...s, _kind: 'signal', _ts: s.ts });
    });

    // Rejections
    (db.data.rejections||[]).filter(r => !date || r.ts?.startsWith(today)).forEach(r => {
      items.push({ ...r, _kind: 'rejection', _ts: r.ts, type: 'skip' });
    });

    // Trades
    (db.data.trades||[]).filter(t => !date || t.ts?.startsWith(today)).forEach(t => {
      items.push({ ...t, _kind: 'trade', _ts: t.ts });
    });

    // Sort newest first
    items.sort((a,b) => (b._ts||'').localeCompare(a._ts||''));

    // Apply filters
    if (scanner && scanner !== 'all') items = items.filter(i => i.scanner === scanner);
    if (ticker) items = items.filter(i => i.ticker?.toUpperCase().includes(ticker.toUpperCase()));
    if (type && type !== 'all') {
      if (type === 'signal')    items = items.filter(i => i._kind === 'signal' && i.type !== 'skip');
      if (type === 'rejection') items = items.filter(i => i._kind === 'rejection' || i.type === 'skip');
      if (type === 'trade')     items = items.filter(i => i._kind === 'trade');
      if (type === 'open')      items = items.filter(i => i.status === 'OPEN' || i.status === 'PARTIAL');
      if (type === 'closed')    items = items.filter(i => i.status === 'CLOSED');
      if (type === 'win')       items = items.filter(i => i.pnl > 0);
      if (type === 'loss')      items = items.filter(i => i.pnl < 0);
    }

    res.json({ items: items.slice(0, parseInt(limit)), total: items.length });
  } catch(e) { res.status(500).json({error:e.message}); }
});


// ════════════════════════════════════════════════════════
// INVESTOR PORTAL — SCANNER INTELLIGENCE + REALLOCATION
// ════════════════════════════════════════════════════════

// Scanner performance stats — visible to investors (no other investor data)
app.get('/api/investor/scanners', auth, (req,res)=>{
  if (req.isAdmin) return res.status(403).json({error:'Use admin endpoint'});
  try {
    const SCANNERS = ['fmp','forex','comm','pa','vp','fb','main'];
    const sLabel = {fmp:'FMP Stocks',forex:'Forex',comm:'Commodity',pa:'Price Action',vp:'Volume Profile',fb:'Failed Breakout',main:'Main'};

    const stats = SCANNERS.map(sc => {
      const trades  = (db.data.trades||[]).filter(t=>t.scanner===sc&&t.status==='CLOSED'&&t.pnl!=null);
      const open    = (db.data.trades||[]).filter(t=>t.scanner===sc&&['OPEN','PARTIAL'].includes(t.status));
      const wins    = trades.filter(t=>t.pnl>0);
      const losses  = trades.filter(t=>t.pnl<=0);
      const totalPnl= trades.reduce((s,t)=>s+t.pnl,0);
      const grossWin= wins.reduce((s,t)=>s+t.pnl,0);
      const grossLoss=Math.abs(losses.reduce((s,t)=>s+t.pnl,0));
      const winRate = trades.length?parseFloat((wins.length/trades.length*100).toFixed(1)):0;
      const pf      = grossLoss>0?parseFloat((grossWin/grossLoss).toFixed(2)):grossWin>0?99:0;

      // Monthly returns for chart
      const monthly = {};
      trades.forEach(t=>{
        const mo=(t.closed_at||t.ts||'').substring(0,7);
        if(!mo)return;
        if(!monthly[mo])monthly[mo]=0;
        monthly[mo]+=t.pnl;
      });
      const monthlyArr = Object.entries(monthly)
        .map(([month,pnl])=>({month,pnl:parseFloat(pnl.toFixed(2))}))
        .sort((a,b)=>a.month.localeCompare(b.month));

      // Running equity
      let peak=0,maxDD=0,runEq=0;
      trades.sort((a,b)=>(a.closed_at||a.ts||'').localeCompare(b.closed_at||b.ts||'')).forEach(t=>{
        runEq+=t.pnl;
        if(runEq>peak)peak=runEq;
        const dd=peak-runEq;if(dd>maxDD)maxDD=dd;
      });

      // Last ping for status
      const ping=(db.data.pings||[]).filter(p=>p.scanner===sc).sort((a,b)=>b.ts.localeCompare(a.ts))[0];
      const alive=ping&&(Date.now()-new Date(ping.ts).getTime())<15*60*1000;

      // Recent trades (last 20, no sensitive data)
      const recentTrades = trades.slice(-20).reverse().map(t=>({
        ticker:t.ticker, direction:t.direction, pnl:t.pnl,
        setup_type:t.setup_type, ts:t.ts, closed_at:t.closed_at,
        status:t.status, entry:t.entry, close_price:t.close_price
      }));

      return {
        scanner: sc, label: sLabel[sc],
        status:  alive?(ping.status||'active'):'offline',
        message: ping?.message||'',
        stats: {
          total_trades:  trades.length,
          open_positions:open.length,
          win_rate:      winRate,
          total_pnl:     parseFloat(totalPnl.toFixed(2)),
          profit_factor: pf,
          avg_win:       wins.length?parseFloat((grossWin/wins.length).toFixed(2)):0,
          avg_loss:      losses.length?parseFloat((grossLoss/losses.length).toFixed(2)):0,
          max_drawdown:  parseFloat(maxDD.toFixed(2)),
        },
        monthly_returns: monthlyArr,
        recent_trades:   recentTrades
      };
    });

    res.json({ scanners: stats });
  } catch(e){ res.status(500).json({error:e.message}); }
});

// Investor's own stake allocation per scanner
app.get('/api/investor/allocation', auth, (req,res)=>{
  if (req.isAdmin) return res.status(403).json({error:'Use admin endpoint'});
  try {
    const inv=(db.data.investors||[]).find(i=>i.id===req.investorId);
    if(!inv) return res.status(404).json({error:'Not found'});

    const myStakes=(db.data.stakes||[]).filter(s=>s.investor_id===inv.id);
    const totalStaked=myStakes.reduce((s,x)=>s+x.amount,0);

    // Current allocation per scanner
    const alloc={};
    myStakes.forEach(s=>{
      if(!alloc[s.scope])alloc[s.scope]=0;
      alloc[s.scope]+=s.amount;
    });

    // Pending reallocations (queued for next day)
    const pending=(db.data.reallocations||[]).filter(r=>r.investor_id===inv.id&&r.status==='PENDING');

    res.json({
      total_staked: totalStaked,
      current_allocation: alloc,
      pending_reallocations: pending,
      stakes: myStakes
    });
  } catch(e){ res.status(500).json({error:e.message}); }
});

// Request reallocation between scanners
// Applies at EOD — doesn't affect today's open trades
app.post('/api/investor/reallocate', auth, async (req,res)=>{
  if (req.isAdmin) return res.status(403).json({error:'Use admin endpoint'});
  try {
    const inv=(db.data.investors||[]).find(i=>i.id===req.investorId);
    if(!inv) return res.status(404).json({error:'Not found'});

    const { from_scanner, to_scanner, amount, notes } = req.body;
    if(!from_scanner||!to_scanner||!amount)
      return res.status(400).json({error:'from_scanner, to_scanner, and amount required'});
    if(from_scanner===to_scanner)
      return res.status(400).json({error:'Cannot move to the same scanner'});

    // Check they have enough in source scanner
    const myStakes=(db.data.stakes||[]).filter(s=>s.investor_id===inv.id);
    const fromBalance=myStakes.filter(s=>s.scope===from_scanner||s.scope==='all')
                               .reduce((s,x)=>s+x.amount,0);
    if(parseFloat(amount)>fromBalance)
      return res.status(400).json({error:`Insufficient balance in ${from_scanner}. Available: £${fromBalance.toFixed(2)}`});

    // Check no pending reallocation already
    if(!db.data.reallocations) db.data.reallocations=[];
    const hasPending=(db.data.reallocations||[]).find(r=>r.investor_id===inv.id&&r.status==='PENDING');
    if(hasPending) return res.status(400).json({error:'You already have a pending reallocation. It will apply tonight at EOD.'});

    // Queue the reallocation
    const realloc = {
      id:'realloc_'+Date.now(),
      investor_id: inv.id,
      investor_name: inv.name,
      from_scanner,
      to_scanner,
      amount: parseFloat(amount),
      notes: notes||'',
      status: 'PENDING',
      requested_at: now(),
      applies_at: null,   // set when applied at EOD
      apply_from: new Date(Date.now()+86400000).toISOString().split('T')[0]  // tomorrow
    };
    db.data.reallocations.push(realloc);
    await save();

    res.json({
      status:'ok',
      message: `Reallocation queued. £${amount} will move from ${from_scanner.toUpperCase()} to ${to_scanner.toUpperCase()} after today's close.`,
      reallocation: realloc
    });
  } catch(e){ res.status(500).json({error:e.message}); }
});

// Cancel pending reallocation
app.post('/api/investor/reallocate/cancel', auth, async (req,res)=>{
  if (req.isAdmin) return res.status(403).json({error:'Use admin endpoint'});
  try {
    if(!db.data.reallocations) db.data.reallocations=[];
    const r=db.data.reallocations.find(r=>r.investor_id===req.investorId&&r.status==='PENDING');
    if(!r) return res.status(404).json({error:'No pending reallocation found'});
    r.status='CANCELLED';
    await save();
    res.json({status:'ok',message:'Reallocation cancelled'});
  } catch(e){ res.status(500).json({error:e.message}); }
});

// Admin: apply all pending EOD reallocations
// Called by n8n at EOD or manually
app.post('/api/admin/apply-reallocations', adminOnly, async (req,res)=>{
  try {
    if(!db.data.reallocations) db.data.reallocations=[];
    const pending=db.data.reallocations.filter(r=>r.status==='PENDING');
    let applied=0, errors=[];

    for(const r of pending){
      try {
        // Remove amount from source scanner stakes
        const myStakes=db.data.stakes.filter(s=>s.investor_id===r.investor_id);
        let remaining=r.amount;

        // Reduce from source stakes (FIFO)
        const sourceStakes=myStakes.filter(s=>s.scope===r.from_scanner||s.scope==='all')
                                   .sort((a,b)=>a.ts.localeCompare(b.ts));
        for(const stake of sourceStakes){
          if(remaining<=0)break;
          const reduce=Math.min(stake.amount,remaining);
          stake.amount=parseFloat((stake.amount-reduce).toFixed(2));
          remaining=parseFloat((remaining-reduce).toFixed(2));
          if(stake.amount<=0){
            db.data.stakes=db.data.stakes.filter(s=>s.id!==stake.id);
          }
        }

        // Create new stake in destination scanner
        if(!db.data.stakes) db.data.stakes=[];
        db.data.stakes.push({
          id: Date.now()+Math.random(),
          investor_id: r.investor_id,
          amount: r.amount,
          scope: r.to_scanner,
          payment_method: 'reallocation',
          notes: `Reallocated from ${r.from_scanner} · ${r.notes||''}`,
          ts: now()
        });

        r.status='APPLIED';
        r.applies_at=now();
        applied++;
      } catch(e){
        errors.push({id:r.id,error:e.message});
        r.status='FAILED';
      }
    }

    await save();
    res.json({status:'ok',applied,errors,total:pending.length});
  } catch(e){ res.status(500).json({error:e.message}); }
});

// Admin: view all pending reallocations
app.get('/api/admin/reallocations', adminOnly, (req,res)=>{
  res.json(db.data.reallocations||[]);
});



// ════════════════════════════════════════════════════════
// INTELLIGENCE ENGINE — P&L RECONCILIATION + PATTERN AI
// ════════════════════════════════════════════════════════

// ── P&L Reconciliation webhook (called by n8n nightly) ──
app.post('/api/reconcile', adminOnly, async (req,res)=>{
  try {
    const { positions } = req.body; // array from Capital.com closed positions
    if (!positions || !Array.isArray(positions)) {
      return res.status(400).json({ error: 'positions array required' });
    }
    let updated = 0, unmatched = 0;
    const touched = [];
    positions.forEach(pos => {
      const dealId   = pos.dealId || pos.deal_id || '';
      const ticker   = (pos.epic || pos.ticker || '').toUpperCase();
      const pnl      = parseFloat(pos.pnl || pos.profit || 0);
      const closePrice = parseFloat(pos.closeLevel || pos.close_price || 0);
      const closedAt   = pos.createdDateUtc || pos.closed_at || now();
      // Same matcher as close and update. The previous predicate was an
      // OR, so a supplied-but-unmatched deal_id fell through to ticker +
      // OPEN status and could close a different trade.
      const trade = findTradeForMutation(db.data.trades, { deal_id: dealId, ticker });
      if (trade) {
        trade.status      = 'CLOSED';
        // Capital's reconcile payload is GROSS. Keep trade.pnl as-is for
        // anything reading it directly, but name it and derive net, so
        // `pnl_net ?? pnl` no longer resolves to a gross figure.
        trade.pnl         = pnl;
        trade.pnl_gross   = pnl;
        trade.pnl_net     = computeNetPnl(trade);
        trade.close_price = closePrice;
        trade.closed_at   = closedAt;
        touched.push(trade);
        updated++;
      } else { unmatched++; }
    });
    // saveTradeHot -> upsertTradePostgres is the ONLY write path whose
    // column list includes pnl_gross/pnl_net. save() -> saveToPostgres
    // omits them, and loadFromPostgres then overwrites the jsonb copies
    // with the NULL columns, erasing the values on the next restart.
    for (const t of touched) await saveTradeHot(t);
    await save();
    res.json({ status:'ok', updated, unmatched, total: positions.length });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// ── Manual P&L update for a single trade ──
app.patch('/api/trades/:id/pnl', adminOnly, async (req,res)=>{
  try {
    const trade = (db.data.trades||[]).find(t => t.id == req.params.id);
    if (!trade) return res.status(404).json({ error: 'Not found' });
    const { pnl, close_price, closed_at, status } = req.body;
    // A manually supplied pnl is treated as GROSS, consistent with the
    // reconcile payload it exists to correct.
    if (pnl       != null) {
      trade.pnl       = parseFloat(pnl);
      trade.pnl_gross = parseFloat(pnl);
      trade.pnl_net   = computeNetPnl(trade);
    }
    if (close_price!= null) trade.close_price = parseFloat(close_price);
    if (closed_at)          trade.closed_at   = closed_at;
    if (status)             trade.status      = status;

    // Feed brain if this edit closed the trade
    if (trade.status === 'CLOSED') {
      const p = Number(trade.pnl ?? 0);
      const risk = Number(trade.risk_amount ?? trade.risk_usd ?? 0);
      const rec = {
        id: 'brain_' + Date.now() + '_' + Math.random().toString(36).slice(2,7),
        scanner: (trade.scanner||'').toLowerCase(),
        setup_type: (trade.setup_type||trade.trade_type||trade.strategy||'').toUpperCase(),
        direction: (trade.direction||'').toUpperCase(),
        ticker: trade.ticker||trade.symbol||'',
        features: brainFeatures(trade),
        outcome: { win: p>0, r_multiple: risk>0?+(p/risk).toFixed(2):null, pnl: +p.toFixed(2) },
        deal_id: trade.deal_id||'',
        recorded_at: now()
      };
      db.data.trade_brain = db.data.trade_brain || [];
      if (rec.deal_id) db.data.trade_brain = db.data.trade_brain.filter(r=>r.deal_id!==rec.deal_id);
      db.data.trade_brain.push(rec);
    }
    await saveTradeHot(trade);
    await save();
    res.json({ status:'ok', trade });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// ── Pattern Intelligence endpoint ──
// Analyses trades and returns structured stats for AI to work with
app.get('/api/intelligence', adminOnly, (req,res)=>{
  try {
    const { scanner='all', days=30, min_trades=3 } = req.query;
    const since = new Date(Date.now() - parseInt(days)*24*60*60*1000).toISOString();
    let trades = (db.data.trades||[]).filter(t =>
      t.status === 'CLOSED' && t.pnl != null && t.ts >= since &&
      (scanner === 'all' || t.scanner === scanner)
    );

    if (!trades.length) return res.json({ trades:[], patterns:{}, summary:{} });

    // ── By setup type ──
    const bySetup = {};
    trades.forEach(t => {
      const k = t.setup_type || 'UNKNOWN';
      if (!bySetup[k]) bySetup[k] = { trades:0, wins:0, losses:0, total_pnl:0, pnls:[] };
      bySetup[k].trades++;
      bySetup[k].total_pnl += t.pnl;
      bySetup[k].pnls.push(t.pnl);
      if (t.pnl > 0) bySetup[k].wins++; else bySetup[k].losses++;
    });
    Object.values(bySetup).forEach(s => {
      s.win_rate    = s.trades ? parseFloat((s.wins/s.trades*100).toFixed(1)) : 0;
      s.avg_pnl     = parseFloat((s.total_pnl/s.trades).toFixed(2));
      s.avg_win     = s.wins ? parseFloat((s.pnls.filter(p=>p>0).reduce((a,b)=>a+b,0)/s.wins).toFixed(2)) : 0;
      s.avg_loss    = s.losses ? parseFloat((Math.abs(s.pnls.filter(p=>p<=0).reduce((a,b)=>a+b,0))/s.losses).toFixed(2)) : 0;
      s.expectancy  = parseFloat((s.win_rate/100*s.avg_win - (1-s.win_rate/100)*s.avg_loss).toFixed(2));
    });

    // ── By hour of day (ET) ──
    const byHour = {};
    trades.forEach(t => {
      const h = new Date(t.ts).toLocaleString('en-US',{timeZone:'America/New_York',hour:'2-digit',hour12:false,hourCycle:'h23'});
      const k = `${h}:00`;
      if (!byHour[k]) byHour[k] = { trades:0, wins:0, total_pnl:0 };
      byHour[k].trades++;
      byHour[k].total_pnl += t.pnl;
      if (t.pnl > 0) byHour[k].wins++;
    });
    Object.values(byHour).forEach(s => {
      s.win_rate = s.trades ? parseFloat((s.wins/s.trades*100).toFixed(1)) : 0;
      s.avg_pnl  = parseFloat((s.total_pnl/s.trades).toFixed(2));
    });

    // ── By scanner ──
    const byScanner = {};
    trades.forEach(t => {
      const k = t.scanner || 'unknown';
      if (!byScanner[k]) byScanner[k] = { trades:0, wins:0, total_pnl:0 };
      byScanner[k].trades++;
      byScanner[k].total_pnl += t.pnl;
      if (t.pnl > 0) byScanner[k].wins++;
    });
    Object.values(byScanner).forEach(s => {
      s.win_rate = s.trades ? parseFloat((s.wins/s.trades*100).toFixed(1)) : 0;
      s.avg_pnl  = parseFloat((s.total_pnl/s.trades).toFixed(2));
    });

    // ── By day of week ──
    const days_names = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const byDay = {};
    trades.forEach(t => {
      const d = days_names[new Date(t.ts).getDay()];
      if (!byDay[d]) byDay[d] = { trades:0, wins:0, total_pnl:0 };
      byDay[d].trades++;
      byDay[d].total_pnl += t.pnl;
      if (t.pnl > 0) byDay[d].wins++;
    });
    Object.values(byDay).forEach(s => {
      s.win_rate = s.trades ? parseFloat((s.wins/s.trades*100).toFixed(1)) : 0;
      s.avg_pnl  = parseFloat((s.total_pnl/s.trades).toFixed(2));
    });

    // ── Regime performance ──
    const byRegime = {};
    trades.forEach(t => {
      const k = t.regime || (db.data.pings||[]).find(p=>p.scanner===t.scanner&&p.ts<=t.ts)?.regime || 'UNKNOWN';
      if (!byRegime[k]) byRegime[k] = { trades:0, wins:0, total_pnl:0 };
      byRegime[k].trades++;
      byRegime[k].total_pnl += t.pnl;
      if (t.pnl > 0) byRegime[k].wins++;
    });
    Object.values(byRegime).forEach(s => {
      s.win_rate = s.trades ? parseFloat((s.wins/s.trades*100).toFixed(1)) : 0;
      s.avg_pnl  = parseFloat((s.total_pnl/s.trades).toFixed(2));
    });

    // ── Streaks ──
    let currentStreak = 0, maxWinStreak = 0, maxLossStreak = 0, cur = 0;
    [...trades].sort((a,b)=>a.ts.localeCompare(b.ts)).forEach(t => {
      if (t.pnl > 0) { cur = cur > 0 ? cur+1 : 1; maxWinStreak = Math.max(maxWinStreak,cur); }
      else            { cur = cur < 0 ? cur-1 : -1; maxLossStreak = Math.max(maxLossStreak,Math.abs(cur)); }
    });
    currentStreak = cur;

    // ── Overall summary ──
    const wins   = trades.filter(t=>t.pnl>0);
    const losses = trades.filter(t=>t.pnl<=0);
    const totalPnl = trades.reduce((s,t)=>s+t.pnl,0);
    const grossWin = wins.reduce((s,t)=>s+t.pnl,0);
    const grossLoss= Math.abs(losses.reduce((s,t)=>s+t.pnl,0));

    // ── Best/worst trades ──
    const sorted = [...trades].sort((a,b)=>b.pnl-a.pnl);
    const best5  = sorted.slice(0,5).map(t=>({ticker:t.ticker,pnl:t.pnl,setup:t.setup_type,scanner:t.scanner,ts:t.ts}));
    const worst5 = sorted.slice(-5).reverse().map(t=>({ticker:t.ticker,pnl:t.pnl,setup:t.setup_type,scanner:t.scanner,ts:t.ts}));

    res.json({
      meta: { total_trades:trades.length, days:parseInt(days), scanner, since },
      summary: {
        total_pnl:    parseFloat(totalPnl.toFixed(2)),
        win_rate:     trades.length ? parseFloat((wins.length/trades.length*100).toFixed(1)) : 0,
        profit_factor:grossLoss>0 ? parseFloat((grossWin/grossLoss).toFixed(2)) : grossWin>0 ? 99 : 0,
        avg_win:      wins.length ? parseFloat((grossWin/wins.length).toFixed(2)) : 0,
        avg_loss:     losses.length ? parseFloat((grossLoss/losses.length).toFixed(2)) : 0,
        expectancy:   parseFloat(((wins.length/trades.length*(grossWin/Math.max(wins.length,1)))-(losses.length/trades.length*(grossLoss/Math.max(losses.length,1)))).toFixed(2)),
        max_win_streak: maxWinStreak,
        max_loss_streak: maxLossStreak,
        current_streak: currentStreak
      },
      patterns: { bySetup, byHour, byScanner, byDay, byRegime },
      best5, worst5,
      raw_trades: trades.map(t=>({
        id:t.id, ticker:t.ticker, scanner:t.scanner, setup_type:t.setup_type,
        direction:t.direction, entry:t.entry, close_price:t.close_price,
        pnl:t.pnl, ts:t.ts, closed_at:t.closed_at,
        rsi:t.rsi, adx:t.adx, volume_ratio:t.volume_ratio, quality_score:t.quality_score
      }))
    });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// ── EOD Report storage ──
app.post('/api/eod', serviceOrAdmin, async (req,res)=>{
  try {
    const { report, date, summary } = req.body;
    if (!db.data.eod_reports) db.data.eod_reports = [];
    db.data.eod_reports.unshift({
      id: 'eod_'+Date.now(),
      date: date || new Date().toISOString().split('T')[0],
      report: report || '',
      summary: summary || {},
      ts: now()
    });
    if (db.data.eod_reports.length > 90) db.data.eod_reports = db.data.eod_reports.slice(0,90);
    await save();
    res.json({ status:'ok' });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

app.get('/api/eod', adminOnly, (req,res)=>{
  res.json(db.data.eod_reports || []);
});

// ── Live positions cache (updated by n8n every 60s) ──
app.post('/api/positions/live', serviceOrAdmin, async (req,res)=>{
  try {
    const { positions } = req.body;
    if (!db.data.live_positions) db.data.live_positions = {};
    db.data.live_positions = {
      positions: positions || [],
      updated_at: now()
    };
    await save();
    res.json({ status:'ok', count: (positions||[]).length });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

app.get('/api/positions/live', adminOnly, (req,res)=>{
  res.json(db.data.live_positions || { positions:[], updated_at:null });
});


// ════════════════════════════════════════════════════════════
// FMP PROXY — shared fleet cache + outbound rate limiting
// ════════════════════════════════════════════════════════════

const fmpCache = new Map();
const fmpQueue = [];
const fmpStats = { requests: 0, hits: 0, misses: 0, outbound: 0, errors: 0, rate_limited: 0 };

// Price ticks refused by the entry/5..entry*5 sanity band. Without this
// the band was doing its job silently, so a feed emitting decimal-shifted
// prices looked identical to a feed that was fine.
const tickRejections = { total: 0, last: null, by_ticker: {} };
function onPriceTickRejected(source) {
  return (r) => {
    tickRejections.total++;
    tickRejections.last = { ...r, source };
    const k = r.ticker || r.trade_id || 'unknown';
    tickRejections.by_ticker[k] = Number(tickRejections.by_ticker[k] || 0) + 1;
    console.warn(`[price-tick] REJECTED source=${source} trade=${r.trade_id} ticker=${r.ticker} ` +
      `price=${r.price} entry=${r.entry} reason=${r.reason}`);
    // The counters above are process-lifetime, so a bad feed is invisible
    // after any restart -- and this process has restarted ~198 times. The
    // journal is append-only and survives. Fire-and-forget: a journal
    // failure must never break the price path that called us.
    // 'price_tick_rejected' is on the KINDS whitelist in event-journal.js;
    // without it journalEvent returns null and the write vanishes silently.
    journalEvent('price_tick_rejected', {
      scanner: r.scanner || null,
      ticker: r.ticker || null,
      payload: { source, trade_id: r.trade_id ?? null, ticker: r.ticker ?? null,
        price: r.price ?? null, entry: r.entry ?? null, reason: r.reason ?? null,
        occurred_at: now() }
    }).catch(() => {});
  };
}
let fmpProcessing = false;
let fmpBackoffUntil = 0;
let fmpBackfillPausedUntil = Date.parse(process.env.FMP_BACKFILL_PAUSED_UNTIL || readLocalEnvValue('FMP_BACKFILL_PAUSED_UNTIL') || '') || 0;
const FMP_BUDGET_PATH = process.env.FMP_BUDGET_PATH || readLocalEnvValue('FMP_BUDGET_PATH') || path.join(DATA_DIR, 'fmp-proxy-budget.json');
const FMP_BACKFILL_DAILY_CAP = Number(process.env.FMP_BACKFILL_DAILY_CAP || readLocalEnvValue('FMP_BACKFILL_DAILY_CAP') || 1500);
const FMP_MIN_INTERVAL_MS = Math.ceil(60_000 / 250);
const FMP_STALE_MAX_MS = 15 * 60_000;
const FMP_QUOTE_TTL_MS = 3 * 60_000;

// ── CALLER TAGGING ──────────────────────────────────────────
// Every proxy call is attributed to a named caller so FMP quota can be
// split by scanner/job. Untagged HTTP calls are rejected EXCEPT on the
// paths below, which have live callers mid-migration:
//   /api/proxy/fmp/quote   mean_reversion via n8n 172.18.0.3 (431 calls in retained logs)
//   /api/proxy/fmp/raw     internal/manual callers (3 calls)
//   /api/proxy/fmp/regime  internal (1 call)
// /api/proxy/fmp/candles has ZERO observed traffic, so the tag is
// enforced there. /api/proxy/fmp/stats makes no upstream call: exempt.
const FMP_GRANDFATHERED_PATHS = new Set([
  '/api/proxy/fmp/quote',
  '/api/proxy/fmp/raw',
  '/api/proxy/fmp/regime'
]);

function fmpCallerTag(req) {
  const raw = String(req.query.caller || req.get('x-fmp-caller') || '').trim().toLowerCase();
  return /^[a-z0-9][a-z0-9_.:-]{0,39}$/.test(raw) ? raw : '';
}

// Returns the caller tag, or null after having already sent a 400.
function requireFmpCaller(req, res) {
  const tag = fmpCallerTag(req);
  if (tag) return tag;
  if (FMP_GRANDFATHERED_PATHS.has(req.path)) return 'legacy_untagged:' + req.path.split('/').pop();
  res.status(400).json({
    error: 'MISSING_CALLER_TAG',
    message: 'FMP proxy requires a caller tag. Add ?caller=<scanner_or_job_name> or the x-fmp-caller header.',
    example: req.path + '?caller=volume',
    grandfathered_paths: [...FMP_GRANDFATHERED_PATHS]
  });
  return null;
}

// Internal (non-HTTP) callers are identified by cache-key prefix, so
// layer1.js and earnings-guard.js need no change at all.
function fmpResolveCaller(cacheKey, explicit) {
  if (explicit) return explicit;
  const k = String(cacheKey || '');
  if (k.startsWith('l1:')) return 'layer1_backfill';
  if (k.startsWith('earnings-calendar:')) return 'earnings_guard';
  if (k.startsWith('regime')) return 'layer4_regime';
  return 'untagged_internal';
}

function fmpDayKey(ts = new Date()) {
  return ts.toISOString().slice(0, 10);
}

function loadFmpBudget() {
  // Archives the previous day before resetting, so a restart across
  // midnight no longer destroys that day's attribution.
  return fmpStore.loadBudget(FMP_BUDGET_PATH);
}

let fmpBudget = loadFmpBudget();

function persistFmpBudget() {
  fmpStore.persistBudget(FMP_BUDGET_PATH, fmpBudget);
}

function refreshFmpBudgetDay() {
  const day = fmpDayKey();
  if (fmpBudget.day !== day) {
    fmpStore.archiveDay(FMP_BUDGET_PATH, fmpBudget);
    fmpBudget = fmpStore.emptyBudget(day, fmpBudget.reset_observed_at || null);
    persistFmpBudget();
  }
}

function fmpBudgetSnapshot() {
  refreshFmpBudgetDay();
  return {
    ...fmpBudget,
    backfill_daily_cap: FMP_BACKFILL_DAILY_CAP,
    backfill_remaining: Math.max(0, FMP_BACKFILL_DAILY_CAP - Number(fmpBudget.backfill_outbound || 0)),
    path: FMP_BUDGET_PATH
  };
}

function fmpApiKey() {
  const cfg = db.data.scanner_config || {};
  return process.env.FMP_API_KEY ||
    process.env.FINANCIALMODELINGPREP_API_KEY ||
    readLocalEnvValue('FMP_API_KEY') ||
    readLocalEnvValue('FMP_KEY') ||
    readLocalEnvValue('FINANCIALMODELINGPREP_API_KEY') ||
    cfg.fmp_api_key ||
    cfg.FMP_API_KEY ||
    '';
}

function refreshFmpBackfillPause() {
  const fromDisk = Date.parse(readLocalEnvValue('FMP_BACKFILL_PAUSED_UNTIL') || '') || 0;
  if (fromDisk > fmpBackfillPausedUntil) fmpBackfillPausedUntil = fromDisk;
}

function fmpCacheGet(key) {
  const hit = fmpCache.get(key);
  if (!hit || hit.expires <= Date.now()) return null;
  fmpStats.hits++;
  return hit;
}

function fmpCacheGetStale(key) {
  const hit = fmpCache.get(key);
  if (!hit) return null;
  const age = Date.now() - Date.parse(hit.cached_at || 0);
  if (!Number.isFinite(age) || age > FMP_STALE_MAX_MS) {
    fmpCache.delete(key);
    return null;
  }
  fmpStats.hits++;
  return hit;
}

function fmpCacheSet(key, ttlMs, value) {
  fmpCache.set(key, { ...value, expires: Date.now() + ttlMs, cached_at: new Date().toISOString() });
}

function runFmpQueue() {
  if (fmpProcessing || !fmpQueue.length) return;
  fmpProcessing = true;
  const wait = Math.max(0, fmpBackoffUntil - Date.now(), FMP_MIN_INTERVAL_MS);
  setTimeout(async () => {
    const job = fmpQueue.shift();
    try {
      fmpStats.outbound++;
      refreshFmpBudgetDay();
      fmpBudget.outbound = Number(fmpBudget.outbound || 0) + 1;
      if (job.traffic === 'backfill') fmpBudget.backfill_outbound = Number(fmpBudget.backfill_outbound || 0) + 1;
      else fmpBudget.live_outbound = Number(fmpBudget.live_outbound || 0) + 1;
      fmpStore.bumpCaller(fmpBudget, job.caller || 'untagged_internal', 'outbound');
      persistFmpBudget();
      const response = await fetch(job.url, { headers: { Accept: 'application/json' } });
      const text = await response.text();
      const headers = {};
      for (const [k, v] of response.headers.entries()) {
        if (/limit|remaining|reset|quota|plan|ratelimit/i.test(k)) headers[k] = v;
      }
      let body;
      try { body = text ? JSON.parse(text) : null; } catch { body = text; }
      if (response.status === 429) {
        fmpStats.rate_limited++;
        fmpBudget.rate_limited = Number(fmpBudget.rate_limited || 0) + 1;
        persistFmpBudget();
        const retryAfter = Number(response.headers.get('retry-after') || 30);
        fmpBackoffUntil = Date.now() + Math.max(5, retryAfter) * 1000;
      }
      job.resolve({ status: response.status, ok: response.ok, headers, body });
    } catch (e) {
      fmpStats.errors++;
      job.reject(e);
    } finally {
      fmpProcessing = false;
      runFmpQueue();
    }
  }, wait);
}

function queueFmp(url, traffic = 'live', caller = 'untagged_internal') {
  return new Promise((resolve, reject) => {
    fmpQueue.push({ url, traffic, caller, resolve, reject });
    runFmpQueue();
  });
}

async function fmpProxyFetch(cacheKey, ttlMs, endpoint, caller) {
  const callerTag = fmpResolveCaller(cacheKey, caller);
  fmpStats.requests++;
  refreshFmpBudgetDay();
  fmpStore.bumpCaller(fmpBudget, callerTag, 'requests');
  persistFmpBudget();
  const cached = fmpCacheGet(cacheKey);
  if (cached) return { ...cached, cache: 'HIT' };
  refreshFmpBackfillPause();
  if (cacheKey.startsWith('l1:') && Date.now() < fmpBackfillPausedUntil) {
    return {
      status: 429,
      ok: false,
      headers: {},
      body: { error: 'FMP backfill paused after upstream 429', paused_until: new Date(fmpBackfillPausedUntil).toISOString() },
      cache: 'PAUSED'
    };
  }
  const isBackfill = cacheKey.startsWith('l1:');
  refreshFmpBudgetDay();
  if (isBackfill && FMP_BACKFILL_DAILY_CAP > 0 && Number(fmpBudget.backfill_outbound || 0) >= FMP_BACKFILL_DAILY_CAP) {
    return {
      status: 429,
      ok: false,
      headers: {},
      body: {
        error: 'FMP backfill daily budget exhausted',
        cap: FMP_BACKFILL_DAILY_CAP,
        used: Number(fmpBudget.backfill_outbound || 0),
        day: fmpBudget.day
      },
      cache: 'BUDGET_BLOCKED'
    };
  }
  fmpStats.misses++;
  const key = fmpApiKey();
  if (!key) {
    const err = new Error('FMP API key not configured on server');
    err.statusCode = 503;
    throw err;
  }
  const sep = endpoint.includes('?') ? '&' : '?';
  const url = `https://financialmodelingprep.com/${endpoint}${sep}apikey=${encodeURIComponent(key)}`;
  const result = await queueFmp(url, isBackfill ? 'backfill' : 'live', callerTag);
  if (result.status === 429) {
    fmpBackfillPausedUntil = Math.max(fmpBackfillPausedUntil, Date.now() + 30 * 60_000);
    if (isBackfill) persistFmpBudget();
    const stale = fmpCacheGetStale(cacheKey);
    if (stale) return { ...stale, cache: 'STALE', stale_reason: 'upstream_429' };
  } else if (result.ok || Number(result.status) < 400) {
    fmpCacheSet(cacheKey, ttlMs, result);
  }
  const total = fmpStats.hits + fmpStats.misses;
  if (total % 25 === 0) {
    console.log(`FMP proxy cache hit rate ${(fmpStats.hits / total * 100).toFixed(1)}% (${fmpStats.hits}/${total}), queue=${fmpQueue.length}`);
  }
  return { ...result, cache: 'MISS' };
}

function fmpFirst(body) {
  if (Array.isArray(body)) return body[0] || null;
  if (Array.isArray(body?.historical)) return body.historical[0] || null;
  return body || null;
}

function fmpNumber(body, ...keys) {
  const row = fmpFirst(body);
  for (const key of keys) {
    const value = Number(row?.[key]);
    if (Number.isFinite(value)) return value;
  }
  return null;
}

function isoDateOffset(days) {
  const d = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
  return d.toISOString().slice(0, 10);
}

async function fetchFmpRegimeBundle() {
  if (fmpBackoffUntil && Date.now() < fmpBackoffUntil) {
    return {
      ok: false,
      status: 429,
      error: 'FMP proxy is in upstream backoff',
      backoff_until: new Date(fmpBackoffUntil).toISOString(),
      statuses: { spy: 429, vix: 429, dxy: 429, spy_history: 429 }
    };
  }
  const from = isoDateOffset(45);
  const to = isoDateOffset(0);
  const [spy, vix, dxy, spyHistory] = await Promise.all([
    fmpProxyFetch('quote:SPY', FMP_QUOTE_TTL_MS, 'stable/quote?symbol=SPY', 'layer4_regime'),
    fmpProxyFetch('quote:%5EVIX', FMP_QUOTE_TTL_MS, 'stable/quote?symbol=%5EVIX', 'layer4_regime'),
    fmpProxyFetch('quote:DXY', FMP_QUOTE_TTL_MS, 'stable/quote?symbol=DXY', 'layer4_regime'),
    fmpProxyFetch(`regime:spy-eod:${from}:${to}`, 10 * 60_000, `stable/historical-price-eod/full?symbol=SPY&from=${from}&to=${to}`, 'layer4_regime')
  ]);
  const failures = [spy, vix, dxy, spyHistory].filter(r => r?.status && Number(r.status) >= 400);
  const spyPrice = fmpNumber(spy.body, 'price', 'close');
  const vixValue = fmpNumber(vix.body, 'price', 'close');
  const dxyValue = fmpNumber(dxy.body, 'price', 'close');
  const history = Array.isArray(spyHistory.body) ? spyHistory.body : (Array.isArray(spyHistory.body?.historical) ? spyHistory.body.historical : []);
  const closes = history.map(r => Number(r.close ?? r.price)).filter(Number.isFinite);
  const base20 = closes.length >= 20 ? closes[19] : null;
  const spyVs20dPct = spyPrice && base20 ? +(((spyPrice - base20) / base20) * 100).toFixed(6) : null;
  const ok = !!(spyPrice || vixValue || dxyValue || spyVs20dPct) && failures.length < 4;
  return {
    ok,
    status: ok ? 200 : (failures[0]?.status || 'failed'),
    cache: [spy.cache, vix.cache, dxy.cache, spyHistory.cache].filter(Boolean).join(','),
    spy_price: spyPrice,
    vix: vixValue,
    dxy: dxyValue,
    spy_vs_20d_pct: spyVs20dPct,
    spy: fmpFirst(spy.body),
    vix_quote: fmpFirst(vix.body),
    dxy_quote: fmpFirst(dxy.body),
    statuses: {
      spy: spy.status,
      vix: vix.status,
      dxy: dxy.status,
      spy_history: spyHistory.status
    }
  };
}

function sendFmpProxy(res, result) {
  res.set('X-FMP-Proxy-Cache', result.cache);
  for (const [k, v] of Object.entries(result.headers || {})) res.set(`X-FMP-${k}`, String(v));
  res.status(result.status || 200).json(result.body);
}

async function fetchYahooQuote(ticker) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?range=1d&interval=1m`;
  const response = await fetch(url, { headers: { Accept: 'application/json', 'User-Agent': 'fund-system/1.0' } });
  const payload = await response.json().catch(() => null);
  const result = payload?.chart?.result?.[0];
  const meta = result?.meta;
  const closes = result?.indicators?.quote?.[0]?.close || [];
  const price = [...closes].reverse().find(Number.isFinite) ?? Number(meta?.regularMarketPrice);
  if (!response.ok || !Number.isFinite(price)) throw new Error(`Yahoo quote unavailable (${response.status})`);
  const previousClose = Number(meta?.chartPreviousClose ?? meta?.previousClose);
  return [{ symbol: ticker, price, previousClose: Number.isFinite(previousClose) ? previousClose : null,
    change: Number.isFinite(previousClose) ? +(price - previousClose).toFixed(6) : null,
    changesPercentage: Number.isFinite(previousClose) && previousClose !== 0 ? +((price - previousClose) / previousClose * 100).toFixed(6) : null,
    timestamp: Number(meta?.regularMarketTime) || Math.floor(Date.now() / 1000), source: 'YAHOO' }];
}

app.get('/api/proxy/fmp/quote', scannerAuth, async (req, res) => {
  try {
    const ticker = String(req.query.ticker || '').trim().toUpperCase();
    if (!/^[A-Z0-9.^-]{1,20}$/.test(ticker)) return res.status(400).json({ error: 'valid ticker required' });
    const caller = requireFmpCaller(req, res);
    if (!caller) return;
    const result = await fmpProxyFetch(`quote:${ticker}`, FMP_QUOTE_TTL_MS, `stable/quote?symbol=${encodeURIComponent(ticker)}`, caller);
    if (Number(result.status) === 429 && !fmpCacheGetStale(`quote:${ticker}`)) {
      res.set('X-Quote-Source', 'YAHOO');
      return res.json(await fetchYahooQuote(ticker));
    }
    res.set('X-Quote-Source', 'FMP');
    sendFmpProxy(res, result);
  } catch (e) {
    try {
      const ticker = String(req.query.ticker || '').trim().toUpperCase();
      res.set('X-Quote-Source', 'YAHOO');
      res.json(await fetchYahooQuote(ticker));
    } catch (fallbackError) {
      res.status(e.statusCode || 500).json({ error: e.message, fallback_error: fallbackError.message });
    }
  }
});

app.get('/api/proxy/fmp/candles', scannerAuth, async (req, res) => {
  try {
    const ticker = String(req.query.ticker || '').trim().toUpperCase();
    const resKey = String(req.query.res || '5min').trim().toLowerCase();
    const allowed = new Set(['1min', '5min', '15min', '30min', '1hour', '4hour']);
    if (!/^[A-Z0-9.^-]{1,20}$/.test(ticker)) return res.status(400).json({ error: 'valid ticker required' });
    if (!allowed.has(resKey)) return res.status(400).json({ error: 'unsupported res; use 1min,5min,15min,30min,1hour,4hour' });
    const caller = requireFmpCaller(req, res);
    if (!caller) return;
    const endpoint = `stable/historical-chart/${encodeURIComponent(resKey)}?symbol=${encodeURIComponent(ticker)}`;
    const result = await fmpProxyFetch(`candles:${ticker}:${resKey}`, 5 * 60_000, endpoint, caller);
    sendFmpProxy(res, result);
  } catch (e) {
    res.status(e.statusCode || 500).json({ error: e.message });
  }
});

app.get('/api/proxy/fmp/regime', scannerAuth, async (req, res) => {
  try {
    const caller = requireFmpCaller(req, res);
    if (!caller) return;
    refreshFmpBudgetDay();
    fmpStore.bumpCaller(fmpBudget, caller, 'requests');
    persistFmpBudget();
    const cached = fmpCacheGet('regime');
    if (cached) {
      res.set('X-FMP-Proxy-Cache', 'HIT');
      return res.json(cached.body);
    }
    fmpStats.requests++;
    fmpStats.misses++;
    const bundle = await fetchFmpRegimeBundle();
    if (!bundle.ok) return res.status(Number(bundle.status) || 502).json({ error: 'FMP regime unavailable', statuses: bundle.statuses });
    const body = {
      spy: bundle.spy,
      vix: bundle.vix_quote,
      dxy: bundle.dxy_quote,
      spy_price: bundle.spy_price,
      vix_value: bundle.vix,
      dxy_value: bundle.dxy,
      spy_vs_20d_pct: bundle.spy_vs_20d_pct,
      statuses: bundle.statuses
    };
    fmpCacheSet('regime', 10 * 60_000, { status: 200, ok: true, headers: {}, body });
    res.set('X-FMP-Proxy-Cache', 'MISS');
    res.json(body);
  } catch (e) {
    res.status(e.statusCode || 500).json({ error: e.message });
  }
});

app.get('/api/proxy/fmp/raw', scannerAuth, async (req, res) => {
  try {
    const caller = requireFmpCaller(req, res);
    if (!caller) return;
    let rawPath = String(req.query.path || '').trim();
    if (!rawPath) return res.status(400).json({ error: 'path required' });
    rawPath = rawPath.replace(/^\/+/, '');
    // FMP now rejects some legacy api/v3 paths upstream. Prefer stable/... paths.
    if (!/^(stable|api\/v3)\/[A-Za-z0-9_./-]+(\?.*)?$/.test(rawPath)) {
      return res.status(400).json({ error: 'path must be an FMP stable/ or api/v3 path; prefer stable/ paths' });
    }
    if (/apikey=/i.test(rawPath)) return res.status(400).json({ error: 'do not include apikey in path' });

    // Upstream params may arrive as SEPARATE query params rather than
    // baked into `path`. n8n expressions cannot survive being encoded
    // into one path string, which is what blocked mean_reversion and
    // volume from migrating. Both forms are supported; `path` carrying
    // its own ?query keeps working byte-for-byte.
    const extras = [];
    for (const [k, v] of Object.entries(req.query || {})) {
      if (k === 'path' || k === 'caller') continue;
      if (!/^[A-Za-z0-9_.-]{1,40}$/.test(k)) {
        return res.status(400).json({ error: `invalid param name: ${k}` });
      }
      if (/^apikey$/i.test(k)) {
        return res.status(400).json({ error: 'do not supply apikey; the proxy injects it' });
      }
      for (const one of (Array.isArray(v) ? v : [v])) {
        if (typeof one !== 'string') {
          return res.status(400).json({ error: `param ${k} must be a scalar string` });
        }
        if (one.length > 200) {
          return res.status(400).json({ error: `param ${k} exceeds 200 chars` });
        }
        extras.push(`${encodeURIComponent(k)}=${encodeURIComponent(one)}`);
      }
    }
    // Sorted so param order does not fragment the cache. With no extras
    // the endpoint is identical to the legacy single-path form, and so
    // is its cache key.
    extras.sort();
    const endpoint = extras.length
      ? rawPath + (rawPath.includes('?') ? '&' : '?') + extras.join('&')
      : rawPath;

    const result = await fmpProxyFetch(`raw:${endpoint}`, 5 * 60_000, endpoint, caller);
    sendFmpProxy(res, result);
  } catch (e) {
    res.status(e.statusCode || 500).json({ error: e.message });
  }
});

app.get('/api/proxy/fmp/stats', serviceOrAdmin, (req, res) => {
  refreshFmpBackfillPause();
  const total = fmpStats.hits + fmpStats.misses;
  res.json({
    ...fmpStats,
    cache_entries: fmpCache.size,
    queue_depth: fmpQueue.length,
    backoff_until: fmpBackoffUntil ? new Date(fmpBackoffUntil).toISOString() : null,
    backfill_paused_until: fmpBackfillPausedUntil ? new Date(fmpBackfillPausedUntil).toISOString() : null,
    daily_budget: fmpBudgetSnapshot(),
    history: fmpStore.readHistory(FMP_BUDGET_PATH).slice(-7),
    grandfathered_paths: [...FMP_GRANDFATHERED_PATHS],
    cache_hit_rate_pct: total ? +(fmpStats.hits / total * 100).toFixed(1) : 0,
    fmp_key_configured: !!fmpApiKey()
  });
});

// Newest Layer 4 regime snapshot, for scanners that need the regime
// without each of them recomputing it (and re-spending FMP quota).
// 503 when none exists — never a fabricated row.
app.get('/api/regime/latest', scannerAuth, async (req, res) => {
  try {
    const rows = await latestRegimeSnapshots(1);
    const { status, body } = shapeRegimeLatest(rows && rows[0], new Date());
    res.status(status).json(body);
  } catch (e) {
    res.status(503).json({ error: 'REGIME_LOOKUP_FAILED', message: e.message });
  }
});

// ════════════════════════════════════════════════════════════
// FLEET DRIFT — expected vs ACTUAL n8n state
//
// Actual state is read from n8n (read-only); nothing here activates,
// deactivates or imports a workflow. Sampling spawns a short-lived
// python reader, so health reads a cached sample rather than paying
// that cost on every request.
// ════════════════════════════════════════════════════════════
let fleetCache = { state: null, at: 0, error: null };
const FLEET_CACHE_TTL_MS = 60_000;
const FLEET_ALERT_THROTTLE_MS = 30 * 60_000;
const fleetAlertSent = new Map();
// Last delivery outcome, so "we alerted" can be distinguished from "we
// tried to alert and it went nowhere".
let fleetAlertDelivery = null;

async function refreshFleetState() {
  const [expected, actual] = await Promise.all([getFleetExpected(), readActualFleet()]);
  const heartbeats = {};
  for (const [k, v] of Object.entries(db.data.heartbeats || {})) {
    const ts = Number(v && v.ts);
    if (Number.isFinite(ts)) heartbeats[k] = ts;
  }
  const state = computeFleetState({ expected, actual, heartbeats, now: new Date() });
  fleetCache = { state, at: Date.now(), error: null };
  await fireFleetDriftAlerts(state);
  return state;
}

async function fireFleetDriftAlerts(state) {
  const now = Date.now();
  for (const a of state.alerts) {
    const last = fleetAlertSent.get(a.scanner) || 0;
    if (now - last < FLEET_ALERT_THROTTLE_MS) continue;
    fleetAlertSent.set(a.scanner, now);
    const lines = [
      'FLEET DRIFT',
      `scanner:  ${a.scanner}`,
      `expected: ${a.expected}`,
      `actual:   ${a.actual}`,
      ...a.reasons.map(r => `- ${r}`)
    ];
    try {
      const r = await sendTelegramAlert(lines.join('\n'));
      const ok = !!(r && r.ok === true);
      fleetAlertDelivery = {
        ok,
        detail: ok ? null : String((r && (r.skipped || r.status)) || 'unknown'),
        scanner: a.scanner,
        at: new Date().toISOString()
      };
      if (!ok) console.error('[fleet] drift alert NOT delivered:', JSON.stringify(fleetAlertDelivery));
    } catch (e) {
      fleetAlertDelivery = { ok: false, detail: e.message, scanner: a.scanner, at: new Date().toISOString() };
      console.error('[fleet] telegram threw:', e.message);
    }
  }
}

app.get('/api/fleet/state', adminOnly, async (req, res) => {
  try {
    const fresh = String(req.query.fresh || '') === '1';
    if (fresh || !fleetCache.state || Date.now() - fleetCache.at > FLEET_CACHE_TTL_MS) {
      await refreshFleetState();
    }
    res.json({
      ...fleetCache.state,
      alert_delivery: fleetAlertDelivery,
      cached_age_s: Math.round((Date.now() - fleetCache.at) / 1000)
    });
  } catch (e) {
    fleetCache = { ...fleetCache, error: e.message };
    res.status(503).json({ error: 'FLEET_STATE_UNAVAILABLE', message: e.message });
  }
});

// Background sample so health has something to report without paying
// the sampling cost inline.
setInterval(() => {
  refreshFleetState().catch(e => {
    fleetCache = { ...fleetCache, error: e.message };
    console.error('[fleet] refresh failed:', e.message);
  });
}, 10 * 60_000).unref?.();
// ════════════════════════════════════════════════════════════
// LIVE POSITION REFRESH
//
// POST /api/positions/live is documented as "updated by n8n every 60s",
// but its only callers are 13 inactive workflow copies whose 1-minute
// trigger also runs /api/reconcile. Rather than activate those, the
// refresh runs here: Capital login, GET positions, write the same
// snapshot that endpoint writes. It never touches /api/reconcile,
// /api/eod, or any scanner workflow.
// ════════════════════════════════════════════════════════════
// 180s, not 60s. The staleness threshold is 600s, so this still
// tolerates two consecutive failures before the feed reads stale, while
// cutting cycles -- and therefore exposure to transient stalls -- by 2/3.
const LIVE_POS_REFRESH_MS = Number(process.env.LIVE_POS_REFRESH_MS || 180_000);
const LIVE_POS_MAX_BACKOFF_MS = 15 * 60_000;
let livePosFailures = 0;
let livePosReauths = 0;
let livePosLastError = null;
let livePosBackoffUntil = 0;
let livePosLastOk = null;

function livePosHeaders(session) {
  return {
    'X-CAP-API-KEY': session.apiKey,
    CST: session.cst,
    'X-SECURITY-TOKEN': session.token,
    Accept: 'application/json'
  };
}

async function refreshLivePositionsOnce() {
  // Shared cached session from layer1.js. Base URL still comes from
  // config there, never hardcoded to the demo host.
  let session = await capitalLogin();
  if (!session.ok) throw new Error(`capital login ${session.status || ''} ${session.error || ''}`.trim());

  let r = await fetch(`${session.baseUrl}/api/v1/positions`,
    { headers: livePosHeaders(session), signal: AbortSignal.timeout(12000) });

  // EXACTLY ONE re-authentication. An expired CST must not become a
  // login loop, so this never retries a second time.
  if (r.status === 401) {
    invalidateCapitalSession();
    session = await capitalLogin();
    if (!session.ok) throw new Error(`capital re-login ${session.status || ''} ${session.error || ''}`.trim());
    livePosReauths++;
    r = await fetch(`${session.baseUrl}/api/v1/positions`,
      { headers: livePosHeaders(session), signal: AbortSignal.timeout(12000) });
  }
  const ct = r.headers.get('content-type') || '';
  if (!ct.includes('json')) throw new Error(`positions returned non-JSON (${r.status} ${ct})`);
  const body = await r.json();
  if (!Array.isArray(body?.positions)) throw new Error('positions payload has no array');

  // Only a SUCCESSFUL fetch touches the snapshot. A failure must never
  // write a fresh updated_at, or the staleness guard silently stops working.
  db.data.live_positions = { positions: body.positions, updated_at: now() };
  await save();
  return body.positions.length;
}

// ════════════════════════════════════════════════════════════
// BROKER BALANCE — observed, never authoritative.
//
// scanner_config.account_size is typed by hand and has never been
// reconciled against the account. Every scanner sizes from it via
// GET /api/config and every heat figure divides by it, so a divergence
// oversizes positions and under-reports heat by the same proportion.
//
// This only OBSERVES. Overwriting account_size would resize every
// position in the fleet without a human deciding, so the divergence is
// surfaced as a warning and the number stays the owner's.
// ════════════════════════════════════════════════════════════
const BROKER_BAL_REFRESH_MS = Number(process.env.BROKER_BAL_REFRESH_MS || 15 * 60_000);
const BROKER_BAL_MAX_BACKOFF_MS = 60 * 60_000;
const ACCOUNT_SIZE_DIVERGENCE_PCT = 5;
let brokerBalFailures = 0, brokerBalBackoffUntil = 0, brokerBalLastError = null;

async function refreshBrokerBalanceOnce() {
  const session = await capitalLogin();
  if (!session?.ok) throw new Error(`capital login ${session?.status ?? ''} ${session?.error ?? ''}`.trim());
  const r = await fetch(`${session.baseUrl}/api/v1/accounts`, {
    headers: {
      'X-CAP-API-KEY': session.apiKey, CST: session.cst,
      'X-SECURITY-TOKEN': session.token, Accept: 'application/json'
    },
    signal: AbortSignal.timeout(10_000)
  });
  if (r.status === 401) { invalidateCapitalSession(); throw new Error('capital 401 on /accounts'); }
  if (!r.ok) throw new Error(`accounts http ${r.status}`);
  const body = await r.json();
  const accounts = Array.isArray(body?.accounts) ? body.accounts : [];
  const preferred = accounts.find(a => a.preferred) || accounts[0];
  const balance = Number(preferred?.balance?.balance);
  if (!Number.isFinite(balance)) throw new Error('no finite balance in /accounts payload');

  // Only a SUCCESSFUL fetch stamps the time. A failure must never make a
  // stale figure look fresh -- the same rule the live-position poller
  // follows for its snapshot.
  db.data.broker_balance = {
    balance,
    currency: preferred?.currency ?? null,
    available: Number(preferred?.balance?.available) || null,
    account_id: preferred?.accountId ?? null,
    observed_at: now()
  };
  await save();
  return balance;
}

setInterval(() => {
  if (Date.now() < brokerBalBackoffUntil) return;
  refreshBrokerBalanceOnce()
    .then(() => { brokerBalFailures = 0; brokerBalLastError = null; brokerBalBackoffUntil = 0; })
    .catch(e => {
      brokerBalFailures++;
      brokerBalLastError = e.message;
      const backoff = Math.min(BROKER_BAL_MAX_BACKOFF_MS,
        BROKER_BAL_REFRESH_MS * Math.pow(2, Math.min(brokerBalFailures, 3)));
      brokerBalBackoffUntil = Date.now() + backoff;
      console.error(`[broker-balance] refresh failed (${brokerBalFailures}): ${e.message} — retrying in ${Math.round(backoff / 1000)}s`);
    });
}, BROKER_BAL_REFRESH_MS).unref?.();

// Compare the typed figure against the observed one. Exported shape is
// used by /api/health and the Controls panel.
function accountSizeDivergence() {
  const cfg = db.data.scanner_config || {};
  const bb = db.data.broker_balance || null;
  const configured = Number(cfg.account_size) || null;
  const observed = bb && Number.isFinite(Number(bb.balance)) ? Number(bb.balance) : null;
  if (!configured || !observed) {
    return { configured, observed, observed_at: bb?.observed_at ?? null,
             divergence_pct: null, stale: true,
             reason: !observed ? 'broker balance not yet observed' : 'account_size not configured' };
  }
  const pct = +(((configured - observed) / observed) * 100).toFixed(2);
  const ageMs = Date.now() - Date.parse(bb.observed_at);
  return {
    configured, observed, currency: bb.currency ?? null,
    observed_at: bb.observed_at,
    observation_age_minutes: Number.isFinite(ageMs) ? Math.round(ageMs / 60000) : null,
    divergence_pct: pct,
    divergent: Math.abs(pct) > ACCOUNT_SIZE_DIVERGENCE_PCT,
    stale: !Number.isFinite(ageMs) || ageMs > 6 * 60 * 60_000,
    risk_per_trade_at_configured: null,   // filled by the caller which knows RISK_PCT
    note: 'account_size is authoritative for sizing and heat; broker_balance is observed only and never overwrites it'
  };
}

setInterval(() => {
  if (Date.now() < livePosBackoffUntil) return;
  refreshLivePositionsOnce()
    .then(n => {
      if (livePosFailures) console.log(`[live-positions] recovered after ${livePosFailures} failure(s)`);
      livePosFailures = 0; livePosLastError = null; livePosBackoffUntil = 0;
      livePosLastOk = new Date().toISOString();
    })
    .catch(e => {
      livePosFailures++;
      livePosLastError = e.message;
      // Back off rather than hammer, but never stop permanently: the cap
      // means a Capital outage delays the feed, it does not disable it.
      const backoff = Math.min(LIVE_POS_MAX_BACKOFF_MS,
        LIVE_POS_REFRESH_MS * Math.pow(2, Math.min(livePosFailures, 4)));
      livePosBackoffUntil = Date.now() + backoff;
      console.error(`[live-positions] refresh failed (${livePosFailures}): ${e.message} — retrying in ${Math.round(backoff / 1000)}s`);
    });
}, LIVE_POS_REFRESH_MS).unref?.();

initFleetStore()
  .then(() => refreshFleetState())
  .catch(e => console.error('[fleet] init failed:', e.message));

// ════════════════════════════════════════════════════════════
// CENTRALIZED CONFIG — single source of truth for all scanners
// ════════════════════════════════════════════════════════════

// Scanners GET this at startup instead of hardcoding values.
// Public (no auth) so n8n can fetch without credentials.
app.get('/api/config', async (req, res) => {
  const base = { ...(db.data.scanner_config || {}) };
  const safeBase=Object.fromEntries(CONFIG_PUBLIC_KEYS.filter(k=>Object.hasOwn(base,k)).map(k=>[k,base[k]]));
  const scanner = req.query.scanner ? String(req.query.scanner) : '';
  if (!scanner) return res.json(safeBase);
  if (!SCANNER_ORDER.includes(scanner)) return res.status(404).json({error:'unknown scanner'});
  try {
    const merged = await getScannerParams(scanner);
    res.json({
      ...safeBase,
      params: merged.params,
      params_updated_at: merged.params_updated_at,
      params_source: Object.keys(merged.params || {}).length ? 'server' : 'local',
      config_hash: await getConfigHash(scanner, base)
    });
  } catch (e) {
    res.status(503).json({ status:'error', reason:'PARAM_STORE_UNAVAILABLE', scanner, params_error:e.message });
  }
});

// Admin updates config from dashboard Settings
app.post('/api/config', adminOnly, async (req, res) => {
  const updates=Object.fromEntries(Object.entries(req.body||{}).filter(([k])=>CONFIG_WRITE_KEYS.has(k)));
  db.data.scanner_config = {
    ...db.data.scanner_config,
    ...updates,
    updated_at: new Date().toISOString()
  };
  await save();
  res.json({ status: 'ok', config: Object.fromEntries(CONFIG_PUBLIC_KEYS.filter(k=>Object.hasOwn(db.data.scanner_config,k)).map(k=>[k,db.data.scanner_config[k]])) });
});

app.post('/api/admin/paper-only', adminOnly, async(req,res)=>{
  const reason=String(req.body?.reason||'').trim();
  if(!reason)return res.status(400).json({error:'reason required'});
  const next=req.body?.paper_only;
  if(typeof next!=='boolean')return res.status(400).json({error:'paper_only boolean required'});
  const old=db.data.scanner_config?.paper_only===true;
  db.data.scanner_config={...(db.data.scanner_config||{}),paper_only:next,updated_at:now()};
  await recordConfigChangelog({parameter:'paper_only',old_value:old,new_value:next,reason});
  await save(); res.json({status:'ok',paper_only:next,old_value:old,reason});
});

// ════════════════════════════════════════════════════════════
// GLOBAL RISK LEDGER — portfolio heat across ALL scanners
// ════════════════════════════════════════════════════════════

// Scanner calls this BEFORE placing an order. Returns allowed:true/false.
// Enforces: master kill switch, paper-only, max global heat %, max positions.
app.post('/api/risk/check', scannerAuth, serializeRiskGate, async (req, res) => {
  const cfg = db.data.scanner_config || {};
  const body = req.body || {};
  const { scanner, ticker, risk_amount = 0 } = body;
  let brain = { verdict: 'INSUFFICIENT', n: 0, pct_won: null, avg_ret_1d: null, sample_ids: [] };
  let brainVetoEnabled = false;
  let scannerParams = {};
  try {
    brain = await similarOutcomeVerdict(body);
    const params = scanner ? await getScannerParams(scanner) : { params: {} };
    scannerParams = params.params || {};
    brainVetoEnabled = scannerParams.BRAIN_VETO_ENABLED === true;
  } catch (e) {
    brain = { verdict: 'ERROR', n: 0, pct_won: null, avg_ret_1d: null, sample_ids: [], error: e.message };
  }
  const withBrain = (payload) => res.json({ ...payload, brain });
  const brainReason = () => `BRAIN_VETO: ${brainStatsText(brain)}`;
  const drawdown = await maybeAlertRiskMult(sendTelegramAlert).catch(e => ({ risk_mult: 1, drawdown_pct: 0, error: e.message }));
  const withLayer3 = (payload) => ({
    risk_mult: drawdown.risk_mult,
    drawdown_pct: drawdown.drawdown_pct,
    risk_mult_alert_sent: drawdown.sent === true,
    ...payload
  });
  const blockBreaker = async (code, detail) => {
    const reason=`SYSTEM_BREAKER:${code}`;
    const rejection = { id:nid('rejections'), ts:now(), scanner:scanner||'unknown', ticker:ticker||'UNKNOWN', reason, detail, measurement_population:'NEVER_ENTERED', config_hash:await getConfigHash(scanner||'unknown', cfg) };
    db.data.rejections.unshift(rejection);
    await saveRejectionHot(rejection);
    await journalEvent('breaker_trip', rejection).catch(()=>{});
    return withBrain(withLayer3({ allowed:false, reason, paper_only:cfg.paper_only===true }));
  };

  if (cfg.kill_switch === true) {
    return withBrain(withLayer3({ allowed: false, reason: 'KILL_SWITCH active - all new orders blocked', paper_only: false }));
  }

  // R1: risk_amount was destructured with a default of 0 and never validated.
  // Omitted -> heat never rises (0 added to currentRisk). Non-numeric ->
  // NaN > maxHeat is FALSE, so the cap silently passes. Reject both here,
  // before risk_amount reaches any arithmetic below.
  const riskAmountNum = Number(risk_amount);
  if (!Number.isFinite(riskAmountNum) || riskAmountNum < 0) {
    return withBrain(withLayer3({
      allowed: false,
      reason: 'INVALID_RISK_AMOUNT: risk_amount must be a finite, non-negative number',
      paper_only: cfg.paper_only === true
    }));
  }

  // R2: etDay is an ET calendar date, but timestamps are stored as UTC ISO
  // strings, so slice(0,10) yielded the UTC date. Between 00:00-04:00 UTC the
  // two disagree, which reset MAX_ORDERS_PER_DAY and MAX_DAILY_LOSS_R at
  // ~20:00 ET -- mid-session. Convert each trade's timestamp to ET before
  // comparing, rather than slicing the raw string.
  const etDay = etDateOf(new Date());
  const scannerTrades = (db.data.trades||[]).filter(t => String(t.scanner||'').toLowerCase()===String(scanner||'').toLowerCase());
  const openedToday = scannerTrades.filter(t => etDateOf(t.opened_at||t.ts)===etDay);
  const maxOrders = Number(scannerParams.MAX_ORDERS_PER_DAY ?? 3);
  if (openedToday.length >= maxOrders) return blockBreaker('MAX_ORDERS_PER_DAY',`${openedToday.length}/${maxOrders}`);
  const closedToday = scannerTrades.filter(t => t.status==='CLOSED' && etDateOf(t.closed_at)===etDay);
  const lossR = Math.abs(closedToday.reduce((s,t) => {
    const risk=Number(t.risk_amount??t.risk_usd??0), pnl=Number(t.pnl??0); return s+(risk>0&&pnl<0?pnl/risk:0);
  },0));
  const maxLossR = Number(scannerParams.MAX_DAILY_LOSS_R ?? 3);
  if (lossR >= maxLossR) return blockBreaker('MAX_DAILY_LOSS_R',`${lossR.toFixed(2)}/${maxLossR}`);
  const resetAt = cfg.loss_halt_reset_at?.[String(scanner||'').toLowerCase()] || '';
  const recentClosed = scannerTrades.filter(t=>t.status==='CLOSED'&&(!resetAt||new Date(t.closed_at)>new Date(resetAt))).sort((a,b)=>new Date(b.closed_at)-new Date(a.closed_at));
  // R3: a null-pnl close used to reset this streak. Number(null) is 0, which is
  // not < 0, so the loop hit `else break` and the streak read zero -- un-halting
  // a scanner that had genuinely lost N in a row. FAIL OPEN. A close with no
  // recorded pnl is an UNKNOWN outcome, not a win: skip it and keep counting.
  // NOTE: Number(null) === 0, which IS finite -- so a bare Number.isFinite()
  // guard does NOT catch a null pnl. null/undefined must be tested before
  // coercion, or the exact bug this fixes survives the fix.
  let consecutiveLosses=0;
  for (const t of recentClosed) {
    if (t.pnl === null || t.pnl === undefined) continue;  // unknown outcome
    const pnl = Number(t.pnl);
    if (!Number.isFinite(pnl)) continue;                  // NaN / Infinity / junk
    if (pnl < 0) consecutiveLosses++; else break;          // 0 = breakeven, correctly ends the streak
  }
  const lossHalt = Number(scannerParams.CONSECUTIVE_LOSS_HALT ?? 5);
  if (consecutiveLosses >= lossHalt) return blockBreaker('CONSECUTIVE_LOSS_HALT',`${consecutiveLosses}/${lossHalt}; manual reset required`);

  const ledger = db.data.risk_ledger || [];
  const acct = cfg.account_size || 10000;
  const assetClass=classifyAssetClass(ticker,scanner);
  let gapMult=Number(scannerParams.GAP_MULT ?? (assetClass==='index'?1.5:assetClass==='commodity'?1.5:assetClass==='forex'?1.2:assetClass==='crypto'?1.3:2));
  if(assetClass==='us_stock'){try{const eg=await guardResult(String(ticker||'').toUpperCase());if(eg.blocked)gapMult=Math.max(gapMult,2);}catch{}}
  const isOvernight=p=>p.overnight===true||String(p.opened_at||'').slice(0,10)<now().slice(0,10);
  const currentRisk = Number(ledger.reduce((s, p) => s + (Number(p.risk_amount) || 0)*(isOvernight(p)?Number(p.gap_mult||({index:1.5,commodity:1.5,forex:1.2,crypto:1.3,us_stock:2}[classifyAssetClass(p.ticker,p.scanner)]||1)):1), 0)) || 0;
  const worstCaseRisk=riskAmountNum*gapMult;
  const currentHeatPct = (currentRisk / acct) * 100;
  const newHeatPct = ((currentRisk + (body.overnight===true?worstCaseRisk:riskAmountNum)) / acct) * 100;
  const maxHeat = cfg.max_global_heat_pct || 20;
  const maxPos = cfg.max_open_positions || 10;
  const heat = correlationHeat(ledger, body, acct);

  if (ledger.length >= maxPos) {
    return withBrain(withLayer3({
      allowed: false,
      reason: `Max global positions reached (${ledger.length}/${maxPos})`,
      current_heat_pct: +currentHeatPct.toFixed(2),
      effective_heat_pct: heat.effective_heat_pct,
      class_direction_count: heat.class_direction_count,
      paper_only: cfg.paper_only === true
    }));
  }

  if (heat.class_direction_count >= 5) {
    return withBrain(withLayer3({
      allowed: false,
      reason: `CONCENTRATION: 5 ${heat.direction.toLowerCase()} ${heat.asset_class} already open`,
      current_heat_pct: +currentHeatPct.toFixed(2),
      projected_heat_pct: +newHeatPct.toFixed(2),
      effective_heat_pct: heat.effective_heat_pct,
      class_direction_count: heat.class_direction_count,
      paper_only: cfg.paper_only === true
    }));
  }

  if (newHeatPct > maxHeat) {
    return withBrain(withLayer3({
      allowed: false,
      reason: `Global heat ${newHeatPct.toFixed(1)}% would exceed ${maxHeat}% cap (current ${currentHeatPct.toFixed(1)}%)`,
      current_heat_pct: +currentHeatPct.toFixed(2),
      effective_heat_pct: heat.effective_heat_pct,
      class_direction_count: heat.class_direction_count,
      paper_only: cfg.paper_only === true
    }));
  }

  if (heat.effective_heat_pct > maxHeat) {
    return withBrain(withLayer3({
      allowed: false,
      reason: `CORRELATION_HEAT: effective heat ${heat.effective_heat_pct.toFixed(1)}% would exceed ${maxHeat}% cap`,
      current_heat_pct: +currentHeatPct.toFixed(2),
      projected_heat_pct: +newHeatPct.toFixed(2),
      effective_heat_pct: heat.effective_heat_pct,
      class_direction_count: heat.class_direction_count,
      paper_only: cfg.paper_only === true
    }));
  }

  if (brain.verdict === 'VETO' && brainVetoEnabled) {
    await journalEvent('brain_veto', { scanner, ticker, payload:{ mode:'enforced', brain } }).catch(()=>{});
    return withBrain(withLayer3({
      allowed: false,
      reason: brainReason(),
      paper_only: false,
      current_heat_pct: +currentHeatPct.toFixed(2),
      projected_heat_pct: +newHeatPct.toFixed(2),
      effective_heat_pct: heat.effective_heat_pct,
      class_direction_count: heat.class_direction_count,
      open_positions: ledger.length,
      brain_veto_enabled: true
    }));
  }

  if (brain.verdict === 'VETO' && !brainVetoEnabled) {
    try {
      const shadowRejection = {
        id: nid('rejections'),
        ts: now(),
        scanner: scanner || 'unknown',
        ticker: ticker || 'UNKNOWN',
        reason: `BRAIN_SHADOW_VETO: ${brainStatsText(brain)}`,
        detail: 'Shadow mode only; risk/check allowed value was not changed.',
        direction: body.direction || '',
        intended_entry: body.intended_entry ?? body.entry ?? null,
        intended_sl: body.intended_sl ?? body.sl ?? null,
        intended_tp1: body.intended_tp1 ?? body.tp1 ?? body.tp ?? null,
        intended_tp2: body.intended_tp2 ?? body.tp2 ?? null,
        rejected_price: body.rejected_price ?? body.entry ?? body.intended_entry ?? null,
        rejected_time: body.ts || now(),
        score: body.score ?? body.quality_score ?? null,
        brain_response: brain,
        shadow_mode: true
        ,measurement_population: 'NEVER_ENTERED'
      };
      db.data.rejections.unshift(shadowRejection);
      if (db.data.rejections.length > 500) db.data.rejections = db.data.rejections.slice(0, 500);
      await saveRejectionHot(shadowRejection);
      await journalEvent('brain_veto', { scanner, ticker, payload:{ mode:'shadow', brain } }).catch(()=>{});
    } catch (e) {
      console.error('BRAIN_SHADOW_VETO log failed:', e.message);
    }
  }

  if(body.probe_only===true&&scanner==='test_harness'&&String(ticker||'').startsWith('ZZ'))return withBrain(withLayer3({allowed:true,probe_only:true,gap_mult:gapMult,worst_case_risk:+worstCaseRisk.toFixed(2),paper_only:cfg.paper_only===true,current_heat_pct:+currentHeatPct.toFixed(2),projected_heat_pct:+newHeatPct.toFixed(2),effective_heat_pct:heat.effective_heat_pct,class_direction_count:heat.class_direction_count,open_positions:ledger.length,brain_veto_enabled:brainVetoEnabled}));
  const reservationId=`risk_${crypto.randomUUID()}`, expiresAt=new Date(Date.now()+60000).toISOString();
  const reservation={scanner,ticker,deal_id:reservationId,reservation_id:reservationId,risk_amount:Number(risk_amount),direction:body.direction||'LONG',asset_class:assetClass,opened_at:now(),status:'PENDING',expires_at:expiresAt,gap_mult:gapMult,overnight:body.overnight===true};
  ledger.push(reservation);
  // Targeted single-row insert, NOT save(). save() syncs every collection in one
  // transaction and holds a DELETE lock on risk_ledger; measured at 3.1s per call
  // and serializing concurrent checks to 15.6s at 5-way. The scanner timeout is 3s.
  try { await insertReservationPostgres(reservation); }
  catch (e) {
    ledger.splice(ledger.indexOf(reservation),1);   // do not hand out a slot we failed to record
    console.error('[PERSISTENCE_FAILURE] reservation', e.message);
    await sendTelegramAlert(`PERSISTENCE FAILURE: risk reservation could not be written (${e.message}). Gate rejected the request.`).catch(()=>{});
    return res.status(500).json({status:'error',reason:'PERSISTENCE_FAILURE',message:e.message});
  }
  return withBrain(withLayer3({
    allowed: true,
    reservation_id: reservationId,
    reservation_expires_at: expiresAt,
    gap_mult: gapMult,
    worst_case_risk: +worstCaseRisk.toFixed(2),
    paper_only: cfg.paper_only === true,
    current_heat_pct: +currentHeatPct.toFixed(2),
    projected_heat_pct: +newHeatPct.toFixed(2),
    effective_heat_pct: heat.effective_heat_pct,
    class_direction_count: heat.class_direction_count,
    open_positions: ledger.length,
    brain_veto_enabled: brainVetoEnabled
  }));
});

app.post('/api/risk/check-legacy', scannerAuth, async (req, res) => {
  const cfg = db.data.scanner_config || {};
  const body = req.body || {};
  const { scanner, ticker, risk_amount = 0 } = body;
  let brain = { verdict: 'INSUFFICIENT', n: 0, pct_won: null, avg_ret_1d: null, sample_ids: [] };
  let brainVetoEnabled = false;
  try {
    brain = await similarOutcomeVerdict(body);
    const params = scanner ? await getScannerParams(scanner) : { params: {} };
    brainVetoEnabled = params.params?.BRAIN_VETO_ENABLED === true;
  } catch (e) {
    brain = { verdict: 'ERROR', n: 0, pct_won: null, avg_ret_1d: null, sample_ids: [], error: e.message };
  }
  const withBrain = (payload) => res.json({ ...payload, brain });
  const brainReason = () => `BRAIN_VETO: ${brainStatsText(brain)}`;

  // Master kill switch
  if (cfg.kill_switch === true) {
    return res.json({ allowed: false, reason: 'KILL_SWITCH active — all new orders blocked', paper_only: false });
  }

  const ledger = db.data.risk_ledger || [];
  const acct = cfg.account_size || 10000;
  const currentRisk = Number(ledger.reduce((s, p) => s + (Number(p.risk_amount) || 0), 0)) || 0;
  const currentHeatPct = (currentRisk / acct) * 100;
  const newHeatPct = ((currentRisk + Number(risk_amount)) / acct) * 100;
  const maxHeat = cfg.max_global_heat_pct || 20;
  const maxPos = cfg.max_open_positions || 10;

  // Max simultaneous positions across all scanners
  if (ledger.length >= maxPos) {
    return res.json({
      allowed: false,
      reason: `Max global positions reached (${ledger.length}/${maxPos})`,
      current_heat_pct: +currentHeatPct.toFixed(2),
      paper_only: cfg.paper_only === true
    });
  }

  // Max combined portfolio heat
  if (newHeatPct > maxHeat) {
    return res.json({
      allowed: false,
      reason: `Global heat ${newHeatPct.toFixed(1)}% would exceed ${maxHeat}% cap (current ${currentHeatPct.toFixed(1)}%)`,
      current_heat_pct: +currentHeatPct.toFixed(2),
      paper_only: cfg.paper_only === true
    });
  }

  res.json({
    allowed: true,
    paper_only: cfg.paper_only === true,
    current_heat_pct: +currentHeatPct.toFixed(2),
    projected_heat_pct: +newHeatPct.toFixed(2),
    open_positions: ledger.length
  });
});

// Scanner registers an opened position into the ledger
app.post('/api/risk/open', scannerAuth, async (req, res) => {
 try {
  const { scanner, ticker, deal_id, direction = 'LONG' } = req.body || {};
  if (!deal_id || deal_id === 'UNKNOWN') return res.status(400).json({ error: 'valid deal_id required' });

  // No default. A missing risk_amount used to become 0, and a zero row is
  // a position the heat cap cannot see -- it consumes a slot while
  // contributing nothing to heat.
  //
  // null/undefined are tested BEFORE coercion on purpose: Number(null) is
  // 0 and IS finite, so Number.isFinite() alone would pass a missing
  // value straight through as zero, which is the bug being removed.
  const rawRisk = req.body?.risk_amount;
  if (rawRisk === undefined || rawRisk === null || rawRisk === '') {
    return res.status(400).json({
      status: 'error', reason: 'MISSING_RISK_AMOUNT',
      message: 'risk_amount is required on risk/open; a reservation of unknown size cannot be counted against heat',
      scanner: scanner ?? null, ticker: ticker ?? null, deal_id
    });
  }
  const riskNum = Number(rawRisk);
  if (!Number.isFinite(riskNum) || riskNum <= 0) {
    return res.status(400).json({
      status: 'error', reason: 'INVALID_RISK_AMOUNT',
      message: `risk_amount must be a positive finite number on risk/open; received ${JSON.stringify(rawRisk)}. Zero remains valid on risk/check, where it means "no position, nothing to reserve".`,
      scanner: scanner ?? null, ticker: ticker ?? null, deal_id
    });
  }
  db.data.risk_ledger = db.data.risk_ledger || [];
  const requested=String(req.body?.reservation_id||'');
  const pending=db.data.risk_ledger.find(p=>p.status==='PENDING'&&new Date(p.expires_at)>new Date()&&((requested&&p.reservation_id===requested)||(!requested&&p.scanner===scanner&&p.ticker===ticker)));
  const snapshot = db.data.risk_ledger;                       // for fail-closed rollback
  const position = {
    scanner, ticker, deal_id,
    risk_amount: riskNum,
    direction,
    asset_class: classifyAssetClass(ticker, scanner),
    opened_at: new Date().toISOString(), status:'OPEN', expires_at:null, reservation_id:pending?.reservation_id||requested||null
  };
  db.data.risk_ledger = db.data.risk_ledger.filter(p => p.deal_id !== deal_id && p!==pending);
  db.data.risk_ledger.push(position);
  // Targeted DELETE+UPSERT, NOT save(). save() syncs every collection in one
  // transaction holding a DELETE lock on risk_ledger; measured 2026-08-08 under
  // a concurrent scanner cycle at p50 5601ms / p95 7189ms on this endpoint,
  // which also blocked /api/risk/check to p95 3198ms.
  try { await openPositionPostgres(position, pending?.deal_id || null); }
  catch (e) {
    db.data.risk_ledger = snapshot;                           // never consume a reservation we failed to record
    console.error('[PERSISTENCE_FAILURE] risk/open', e.message);
    await sendTelegramAlert(`PERSISTENCE FAILURE: risk/open could not record ${deal_id} (${e.message}). Position NOT registered; ledger unchanged.`).catch(()=>{});
    return res.status(500).json({ status:'error', reason:'PERSISTENCE_FAILURE', message:e.message });
  }
  res.json({ status: 'ok', open_positions: db.data.risk_ledger.length });
 } catch(error) { res.status(500).json({ status:'error', reason:'PERSISTENCE_FAILURE', message:error.message }); }
});

// Reservation watchdog. Separate from the sweeper so it still fires when the
// sweeper cannot clear rows. Telegram is throttled to once per 30 min per
// condition so a stuck ledger does not spam.
let lastReservationAlertAt = 0;
setInterval(async () => {
  try {
    const h = await buildHealthPayload();
    const bad = [...(h.issues||[]), ...(h.warnings||[])]
      .filter(i => i === 'pending_reservations_high' || i === 'reservation_stuck');
    if (!bad.length) { lastReservationAlertAt = 0; return; }
    if (Date.now() - lastReservationAlertAt < 30 * 60_000) return;
    lastReservationAlertAt = Date.now();
    const msg = `RISK RESERVATION ALERT: ${bad.join(', ')} — pending=${h.counts.pending_reservations}, ` +
      `oldest=${h.counts.oldest_reservation_age_s}s, open_positions=${h.counts.risk_positions}/` +
      `${db.data.scanner_config?.max_open_positions ?? 10}. Reservations hold position slots; ` +
      `if they are not converting, scanners will be blocked.`;
    console.error('[reservation-watchdog]', msg);
    await sendTelegramAlert(msg).catch(() => {});
  } catch (e) { console.error('[reservation-watchdog]', e.message); }
}, 60_000).unref?.();

setInterval(async()=>{
  try {
    const before=(db.data.risk_ledger||[]).length;
    db.data.risk_ledger=(db.data.risk_ledger||[]).filter(p=>p.status!=='PENDING'||new Date(p.expires_at)>new Date());
    // Targeted DELETE rather than save(), same reason as the reservation write.
    const swept=await sweepExpiredReservationsPostgres();
    if(swept.length||db.data.risk_ledger.length!==before)
      console.log('[risk-reservation-sweeper] released',swept.length,'reservation(s)');
  } catch(e){ console.error('[risk-reservation-sweeper]',e.message); }
},15000).unref?.();

// Scanner clears a closed position from the ledger
app.post('/api/risk/close', scannerAuth, async (req, res) => {
 try {
  const { deal_id } = req.body || {};
  if (!deal_id || deal_id === 'UNKNOWN') return res.status(400).json({ error: 'valid deal_id required' });
  const ledger = db.data.risk_ledger || [];
  const existing = ledger.find(p => p.deal_id === deal_id);
  // No match: skip the write entirely. A nonexistent deal_id previously paid
  // the full save() (~3.2-3.9s isolated, measured up to 43s under organic
  // production load) to change nothing. Confirmed no scanner or manager
  // relies on this call ever mutating state when the id is unknown.
  if (!existing) return res.json({ status: 'ok', open_positions: ledger.length, matched: false });
  db.data.risk_ledger = ledger.filter(p => p !== existing);
  // Targeted single-row delete, NOT save(). Same reasoning as risk/open: risk/close
  // is the last save() on the risk path and organic risk/close latency (25 samples,
  // 2026-06-10 to 2026-08-08 11:23Z, excludes this session's own test traffic) was
  // p50=7437ms p95=42941ms max=43780ms, with 11/25 (44%) exceeding the scanners'
  // 8000ms httpRequest timeout -- a real, currently-occurring exit-path failure mode.
  try { await closePositionPostgres(deal_id); }
  catch (e) {
    db.data.risk_ledger = ledger;   // restore: never drop a position we failed to persist removing
    console.error('[PERSISTENCE_FAILURE] risk/close', e.message);
    await sendTelegramAlert(`PERSISTENCE FAILURE: risk/close could not remove ${deal_id} (${e.message}). Ledger unchanged; position still counted as open.`).catch(()=>{});
    return res.status(500).json({ status:'error', reason:'PERSISTENCE_FAILURE', message:e.message });
  }
  res.json({ status: 'ok', open_positions: db.data.risk_ledger.length, matched: true });
 } catch(error) { res.status(500).json({ status:'error', reason:'PERSISTENCE_FAILURE', message:error.message }); }
});

// Dashboard reads current global risk state
app.get('/api/risk-status', adminOnly, (req, res) => {
  const cfg = db.data.scanner_config || {};
  const ledger = db.data.risk_ledger || [];
  const acct = cfg.account_size || 10000;
  const currentRisk = Number(ledger.reduce((s, p) => s + (Number(p.risk_amount) || 0), 0)) || 0;
  res.json({
    open_positions: ledger.length,
    max_positions: cfg.max_open_positions || 10,
    current_heat_pct: +((currentRisk / acct) * 100).toFixed(2),
    max_heat_pct: cfg.max_global_heat_pct || 20,
    total_risk: +currentRisk.toFixed(2),
    kill_switch: cfg.kill_switch === true,
    paper_only: cfg.paper_only === true,
    positions: ledger
  });
});

// ════════════════════════════════════════════════════════════
// HEARTBEAT — dead-scanner detection
// ════════════════════════════════════════════════════════════

// Scanner pings this every cycle. Dashboard flags scanners that go silent.
app.post('/api/heartbeat', scannerAuth, async (req, res) => {
  const { scanner, status = 'running', message = '' } = req.body || {};
  if (!scanner) return res.status(400).json({ error: 'scanner required' });
  db.data.heartbeats = db.data.heartbeats || {};
  const heartbeat = {
    ts: Date.now(),
    iso: new Date().toISOString(),
    status,
    msg: message
  };
  db.data.heartbeats[scanner] = heartbeat;
  await saveHeartbeat(scanner, heartbeat);
  res.json({ status: 'ok' });
});

// Dashboard reads heartbeat health. Flags STALE if no ping in 30 min.
app.get('/api/heartbeat/status', (req, res) => {
  const hb = db.data.heartbeats || {};
  const STALE_MS = 30 * 60 * 1000; // 30 minutes
  const now = Date.now();
  const out = {};
  for (const [scanner, data] of Object.entries(hb)) {
    const ageMin = Math.round((now - data.ts) / 60000);
    out[scanner] = {
      ...data,
      age_minutes: ageMin,
      health: (now - data.ts) > STALE_MS ? 'STALE' : 'ALIVE'
    };
  }
  res.json(out);
});

async function buildHealthPayload() {
  const trades = db.data.trades || [];
  const tradeOpen = trades.filter(t => ['OPEN','PARTIAL'].includes(t.status)).length;
  const riskLedger = db.data.risk_ledger || [];
  const pendingReservations = riskLedger.filter(p => p.status === 'PENDING').length;
  const riskOpen = riskLedger.filter(p => p.status !== 'PENDING').length;
  const livePositions = Array.isArray(db.data.live_positions?.positions) ? db.data.live_positions.positions : [];
  const unmanagedPositionEpics = new Set(
    String(process.env.UNMANAGED_POSITION_EPICS || '')
      .split(',').map(v => v.trim().toUpperCase()).filter(Boolean)
  );
  const positionEpic = p => String(p?.market?.epic || p?.position?.epic || p?.epic || '').toUpperCase();
  const unmanagedLive = livePositions.filter(p => unmanagedPositionEpics.has(positionEpic(p)));
  const liveOpen = livePositions.length - unmanagedLive.length;
  // Reconcile by EPIC, not by count. A count comparison masked an
  // untracked unstopped RKLB for five days, then falsely alarmed on a
  // stale US500 the broker had already closed.
  const positionRecon = reconcilePositions({
    trades,
    livePositions,
    unmanagedEpics: unmanagedPositionEpics,
    updatedAt: db.data.live_positions?.updated_at || null,
    now: new Date()
  });
  const heartbeatCount = Object.keys(db.data.heartbeats || {}).length;
  // Reservation health. A reservation is a 60s hold on a position slot taken by
  // /api/risk/check. Sustained PENDING means risk/check is answering but
  // /api/risk/open is not converting — the ledger fills and every scanner blocks.
  const nowMs = Date.now();
  const reservationAges = riskLedger
    .filter(p => p.status === 'PENDING')
    .map(p => (nowMs - new Date(p.opened_at || p.expires_at || nowMs).getTime()) / 1000)
    .filter(Number.isFinite);
  const oldestReservationAgeS = reservationAges.length ? Math.round(Math.max(...reservationAges)) : 0;
  const issues = [];
  // ISSUE, not a warning: running on the mirror means Postgres is no longer
  // the in-memory source, and the next save() writes the mirror back over it.
  if (postgresLoadFailure) {
    issues.push(`postgres_load_failed:${postgresLoadFailure.error}`);
    if (!postgresLoadAlerted) {
      postgresLoadAlerted = true;
      sendTelegramAlert(`POSTGRES LOAD FAILED at ${postgresLoadFailure.at} — running from fund.json.\n` +
        `${postgresLoadFailure.error}\n` +
        `The mirror is now the in-memory source and save() will write it back to Postgres. Restart once the cause is fixed.`)
        .catch(() => {});
    }
  }
  if (tradeOpen !== riskOpen) issues.push('trade_risk_position_mismatch');
  // Set reconciliation replaces the count comparison. When the snapshot
  // is stale the reconciler refuses to compare and emits a staleness
  // warning instead of a verdict -- a dead feed must not look like a
  // real discrepancy.
  issues.push(...positionRecon.issues);
  if (heartbeatCount < 7) issues.push('missing_scanner_heartbeats');
  if (!SCANNER_API_KEY) issues.push('scanner_api_key_not_enforced');
  if (!corsOrigins.length) issues.push('cors_allowlist_not_configured');
  // Only reservation_stuck is a health ISSUE (=> 503). Reservations expire at
  // 60s, so >90s means the sweeper is failing — a real fault. A high pending
  // count is normal during a concurrent scanner cycle and would flap health,
  // so it is surfaced as a warning + Telegram rather than a 503.
  if (oldestReservationAgeS > 90) issues.push('reservation_stuck');
  // A halted fleet is not healthy: 503 is correct here, deliberately
  // unlike the pending-reservation and drift warnings.
  const haltCfg = db.data.scanner_config || {};
  if (haltCfg.kill_switch === true) issues.push('kill_switch_active');
  const warnings = [];
  if (haltCfg.paper_only === true) warnings.push('paper_only_active');
  // A failing refresh must be visible: without this the snapshot simply
  // ages and only the staleness guard would eventually notice.
  if (livePosFailures > 0) warnings.push(`live_positions_refresh_failures:${livePosFailures}`);
  if (pendingReservations > 3) warnings.push('pending_reservations_high');
  // scanner_errors_24h is a WARNING, never an issue -- a burst of reported
  // scanner-side failures should not itself take the dashboard to 503.
  const scannerErrors = await scannerErrorStats().catch(() => ({ total: 0, by_scanner_stage: [] }));
  if (scannerErrors.total > 0) warnings.push('scanner_errors_present');

  // Acting without recording. NOT "few rejections" -- a scanner may
  // legitimately reject nothing. This fires only when the trading path
  // ran and produced no signal, no rejection and no trade at all.
  const tgap = await telemetryGaps().catch(() => null);
  if (tgap?.gaps?.length) {
    for (const g of (tgap.scanners||[]).filter(x=>x.gap)) warnings.push(`telemetry_gap:${g.scanner}:${g.gap_type}`);
  }

  // Name BOTH figures in the warning. "account_size_divergent" alone
  // sends the reader hunting; the numbers are the point.
  const acctDiv = accountSizeDivergence();
  if (acctDiv.divergent) {
    warnings.push(`account_size_divergent:configured=${acctDiv.configured} vs broker=${acctDiv.observed} (${acctDiv.divergence_pct > 0 ? '+' : ''}${acctDiv.divergence_pct}%)`);
  }
  if (acctDiv.stale && acctDiv.observed === null) warnings.push('broker_balance_never_observed');
  else if (acctDiv.stale) warnings.push('broker_balance_stale');
  if (brokerBalFailures > 0) warnings.push(`broker_balance_refresh_failures:${brokerBalFailures}`);
  warnings.push(...positionRecon.warnings);
  // Outcome rows that exist but were never labelled. A WARNING, never an
  // issue: this must not flap health to 503.
  try {
    const bl = await outcomeBacklogCounts();
    if (bl) {
      if (bl.unlabeled_partial > 0) warnings.push(`unlabeled_partial:${bl.unlabeled_partial}`);
      if (bl.parked_skips > 0)      warnings.push(`parked_skips:${bl.parked_skips}`);
      if (bl.deferred_skips > 0)    warnings.push(`deferred_skips:${bl.deferred_skips}`);
      if (bl.never_attempted > 0)   warnings.push(`never_attempted:${bl.never_attempted}`);
    }
  } catch (e) {
    warnings.push('outcome_backlog_unavailable');
  }
  if (tickRejections.total > 0) warnings.push(`price_tick_rejections:${tickRejections.total}`);
  // Fleet drift is a WARNING, never an issue: health must not flap to 503
  // because a workflow version drifted. Weekend-awareness lives in
  // computeFleetState, so a weekday-only scanner idle on Sunday is silent.
  if (fleetCache.state) warnings.push(...fleetWarnings(fleetCache.state));
  // An alert that cannot be delivered is not an alert.
  if (fleetAlertDelivery && fleetAlertDelivery.ok === false) {
    warnings.push(`fleet_alert_undelivered:${fleetAlertDelivery.detail}`);
  }
  else if (fleetCache.error) warnings.push('fleet_state_unavailable');
  return {
    status: issues.length ? 'degraded' : 'ok',
    timestamp: now(),
    storage: postgresEnabled ? (dualWriteEnabled ? 'postgres_dual_write' : 'postgres') : 'json',
    counts: {
      trades: trades.length,
      open_trades: tradeOpen,
      risk_positions: riskOpen,
      pending_reservations: pendingReservations,
      oldest_reservation_age_s: oldestReservationAgeS,
      live_positions: liveOpen,
      unmanaged_live_positions: unmanagedLive.length,
      scanner_heartbeats: heartbeatCount,
      scanner_errors_24h: scannerErrors.total,
      price_tick_rejections: tickRejections.total
    },
    unmanaged_position_epics: [...unmanagedPositionEpics],
    kill_switch: haltCfg.kill_switch === true,
    paper_only: haltCfg.paper_only === true,
    halt_updated_by: haltCfg.halt_updated_by || null,
    halt_reason: haltCfg.halt_reason || null,
    account_size: accountSizeDivergence(),
    telemetry_gap: await telemetryGaps().catch(e => ({ error: e.message })),
    live_positions_last_ok: livePosLastOk,
    live_positions_failures: livePosFailures,
    live_positions_last_error: livePosLastError,
    position_reconciliation: positionRecon,
    warnings,
    issues
  };
}

app.get('/api/health', async (req, res) => {
  const payload = await buildHealthPayload();
  res.status(payload.issues.length ? 503 : 200).json(payload);
});

app.get('/api/health/details', adminOnly, (req, res) => {
  const openTrades = (db.data.trades || []).filter(t => ['OPEN','PARTIAL'].includes(t.status));
  const invalidOpenTrades = openTrades.filter(t =>
    !t.ticker || t.ticker === 'UNKNOWN' || !t.deal_id || t.deal_id === 'UNKNOWN'
  );
  res.json({
    scanner_config: db.data.scanner_config,
    collections: Object.fromEntries(Object.entries(db.data).map(([key, value]) => [
      key,
      Array.isArray(value) ? value.length : (value && typeof value === 'object' ? Object.keys(value).length : null)
    ])),
    consistency: {
      open_trades: openTrades.length,
      invalid_open_trades: invalidOpenTrades.length,
      risk_positions: (db.data.risk_ledger || []).length,
      live_positions: Array.isArray(db.data.live_positions?.positions) ? db.data.live_positions.positions.length : 0
    },
    security: {
      scanner_api_key_enforced: Boolean(SCANNER_API_KEY),
      cors_allowlist_configured: corsOrigins.length > 0,
      default_admin_pin_in_use: !process.env.ADMIN_PIN || process.env.ADMIN_PIN === '1234'
    }
  });
});

// ════════════════════════════════════════════════════════════
// TRADE BRAIN — knowledge base of past trades
// Stores closed trades with features + outcome. New signals query
// it to learn how similar setups performed historically.
// This is retrieval-based learning (not a trained model): explainable,
// works from trade #1, and updates instantly as trades close.
// ════════════════════════════════════════════════════════════

// Helper: normalize a signal/trade into a feature vector the brain can match on
function brainFeatures(t) {
  const num = (v) => (v === undefined || v === null || v === '' || isNaN(+v)) ? null : +v;
  // Bucket continuous values so "similar" is meaningful, not exact-match
  const rsi = num(t.rsi);
  const vol = num(t.volume_ratio ?? t.vol_ratio);
  const gap = num(t.gap_pct);
  const adx = num(t.adx);
  const fromOpen = num(t.from_open_pct);
  const vix = num(t.vix_level ?? t.vix);
  return {
    scanner: (t.scanner || '').toLowerCase(),
    setup_type: (t.setup_type || t.trade_type || t.strategy || '').toUpperCase(),
    direction: (t.direction || '').toUpperCase(),
    htf_bias: (t.htf_bias || '').toUpperCase(),
    spy_regime: (t.spy_regime || t.regime || '').toUpperCase(),
    // Buckets
    rsi_bucket: rsi === null ? null : rsi < 30 ? 'OVERSOLD' : rsi < 45 ? 'LOW' : rsi <= 55 ? 'MID' : rsi <= 70 ? 'HIGH' : 'OVERBOUGHT',
    vol_bucket: vol === null ? null : vol < 1 ? 'LOW' : vol < 1.5 ? 'NORMAL' : vol < 2.5 ? 'ELEVATED' : 'SURGE',
    gap_bucket: gap === null ? null : Math.abs(gap) < 2 ? 'SMALL' : Math.abs(gap) < 5 ? 'MED' : Math.abs(gap) < 10 ? 'LARGE' : 'HUGE',
    adx_bucket: adx === null ? null : adx < 20 ? 'WEAK' : adx < 30 ? 'MOD' : 'STRONG',
    vix_bucket: vix === null ? null : vix < 15 ? 'CALM' : vix < 22 ? 'NORMAL' : vix < 30 ? 'ELEVATED' : 'PANIC',
    // raw values kept for display
    _raw: { rsi, vol, gap, adx, fromOpen, vix }
  };
}

// Similarity score between two feature sets (0..1). Categorical match + bucket match.
function brainSimilarity(a, b) {
  let score = 0, weight = 0;
  const cmp = (key, w) => {
    if (a[key] === null || b[key] === null || a[key] === undefined || b[key] === undefined) return;
    weight += w;
    if (a[key] === b[key]) score += w;
  };
  // Heavy weight on the structural setup, lighter on market context
  cmp('setup_type', 3);
  cmp('direction', 2);
  cmp('scanner', 1.5);
  cmp('htf_bias', 1.5);
  cmp('spy_regime', 1);
  cmp('rsi_bucket', 1.5);
  cmp('vol_bucket', 1.5);
  cmp('gap_bucket', 1);
  cmp('adx_bucket', 1);
  cmp('vix_bucket', 1);
  return weight === 0 ? 0 : score / weight;
}

// Record a closed trade into the brain
app.post('/api/brain/record', scannerAuth, async (req, res) => {
  const t = req.body || {};
  // Determine outcome
  const pnl = Number(t.pnl ?? t.pnl_realised ?? 0);
  let rMultiple = t.r_multiple;
  if (rMultiple === undefined || rMultiple === null) {
    const risk = Number(t.risk_amount ?? t.risk_usd ?? 0);
    rMultiple = risk > 0 ? +(pnl / risk).toFixed(2) : null;
  }
  const win = t.win !== undefined ? !!t.win : (pnl > 0);

  const record = {
    id: 'brain_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
    scanner: (t.scanner || '').toLowerCase(),
    setup_type: (t.setup_type || t.trade_type || t.strategy || '').toUpperCase(),
    direction: (t.direction || '').toUpperCase(),
    ticker: t.ticker || t.symbol || t.pair || t.epic || '',
    features: brainFeatures(t),
    outcome: { win, r_multiple: rMultiple, pnl: +pnl.toFixed(2) },
    deal_id: t.deal_id || t.dealId || '',
    recorded_at: new Date().toISOString()
  };
  db.data.trade_brain = db.data.trade_brain || [];
  // De-dupe by deal_id if present
  if (record.deal_id) {
    db.data.trade_brain = db.data.trade_brain.filter(r => r.deal_id !== record.deal_id);
  }
  db.data.trade_brain.push(record);
  await save();
  res.json({ status: 'ok', total_memories: db.data.trade_brain.length });
});

// Query the brain: signal_outcomes neighbor verdict for a new candidate.
app.post('/api/brain/similar', scannerAuth, async (req, res) => {
  try {
    res.json(await similarOutcomeVerdict(req.body || {}));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Brain stats — overview of what the brain has learned, broken down
app.get('/api/brain/stats', (req, res) => {
  const brain = db.data.trade_brain || [];
  const total = brain.length;
  if (total === 0) return res.json({ total: 0, by_setup: [], by_scanner: [], message: 'Brain is empty — close some trades to start learning.' });

  const group = (keyFn) => {
    const m = {};
    brain.forEach(r => {
      const k = keyFn(r) || 'UNKNOWN';
      m[k] = m[k] || { key: k, n: 0, wins: 0, pnl: 0, rSum: 0, rN: 0 };
      m[k].n++; if (r.outcome.win) m[k].wins++;
      m[k].pnl += r.outcome.pnl || 0;
      if (r.outcome.r_multiple !== null && !isNaN(r.outcome.r_multiple)) { m[k].rSum += r.outcome.r_multiple; m[k].rN++; }
    });
    return Object.values(m).map(g => ({
      key: g.key, samples: g.n,
      win_rate: Math.round((g.wins / g.n) * 100),
      avg_r: g.rN ? +(g.rSum / g.rN).toFixed(2) : null,
      total_pnl: +g.pnl.toFixed(2)
    })).sort((a, b) => b.samples - a.samples);
  };

  res.json({
    total,
    overall_win_rate: Math.round((brain.filter(r => r.outcome.win).length / total) * 100),
    by_setup: group(r => r.setup_type),
    by_scanner: group(r => r.scanner),
    by_regime: group(r => r.features.spy_regime),
    by_time: group(r => {
      const f = r.features._raw.fromOpen;
      return f === null ? null : f < 1 ? 'First hour' : f < 3 ? 'Mid-morning' : f < 5 ? 'Midday' : 'Afternoon';
    })
  });
});

app.get('/api/intelligence/brain/stats', adminOnly, async (_req, res) => {
  try {
    res.json({
      mode: 'shadow',
      metric: 'BRAIN_SHADOW_VETO rows joined to signal_outcomes; actually_lost means ret_1d < 0',
      ...(await brainShadowStats())
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/intelligence/strategy-health/run', adminOnly, async (_req, res) => {
  try {
    res.json(await runStrategyHealth(db, { sendTelegramAlert, save }));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/intelligence/strategy-health', adminOnly, async (_req, res) => {
  try {
    res.json(await latestStrategyHealth());
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/intelligence/equity/run', adminOnly, async (_req, res) => {
  try {
    const equity = await runEquityCurve(db);
    const throttle = await maybeAlertRiskMult(sendTelegramAlert);
    res.json({ equity, throttle });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/intelligence/equity/throttle', adminOnly, async (_req, res) => {
  try {
    res.json(await drawdownState());
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── Serve frontend ───────────────────────────────────────────
app.get('/investor', (req,res) => res.sendFile(path.join(__dirname,'../public/investor.html')));

// ── Serve frontend (catch-all MUST be last) ───
// ------------------------------------------------------------
// PERFORMANCE ANALYTICS ROUTES  v1.0
// Paste this entire block into server/index.js
// BEFORE your existing app.listen() line
// ------------------------------------------------------------
//
// Reads from your existing lowdb data (trades, signals, rejections)
// and computes all the metrics needed for the analytics dashboard.
// NO schema changes needed � works with existing data structure.
// ------------------------------------------------------------

// ------------------------------------------------------------
// PERFORMANCE ANALYTICS ROUTES  v2.0  (fund-system compatible)
// Uses db.data directly — no getDb() needed
// No auth required — analytics.html is a separate public page
// ------------------------------------------------------------

function _n(v) { return isFinite(v) && v !== null ? +v : 0; }
function _r2(v) { return Math.round(_n(v) * 100) / 100; }
function _dateStr(ts) { return new Date(ts).toISOString().slice(0, 10); }

function _computeMetrics(trades) {
  const closed = trades.filter(t => t.status === 'CLOSED' && t.pnl !== undefined && t.pnl !== null);
  if (!closed.length) return null;
  const wins  = closed.filter(t => _n(t.pnl) > 0);
  const losses= closed.filter(t => _n(t.pnl) <= 0);
  const totalPnl    = closed.reduce((s,t) => s + _n(t.pnl), 0);
  const grossWin    = wins.reduce((s,t)   => s + _n(t.pnl), 0);
  const grossLoss   = Math.abs(losses.reduce((s,t) => s + _n(t.pnl), 0));
  const winRate     = closed.length ? (wins.length / closed.length) * 100 : 0;
  const avgWin      = wins.length   ? grossWin  / wins.length   : 0;
  const avgLoss     = losses.length ? grossLoss / losses.length : 0;
  const rr          = avgLoss ? avgWin / avgLoss : 0;
  const profitFactor= grossLoss ? grossWin / grossLoss : grossWin > 0 ? 99.99 : 0;
  let peak = 0, equity = 0, maxDD = 0;
  for (const t of closed) {
    equity += _n(t.pnl);
    if (equity > peak) peak = equity;
    const dd = peak - equity;
    if (dd > maxDD) maxDD = dd;
  }
  const withTimes = closed.filter(t => t.opened_at && t.closed_at);
  const avgHoldMin = withTimes.length
    ? withTimes.reduce((s,t) => s + (new Date(t.closed_at) - new Date(t.opened_at)) / 60000, 0) / withTimes.length
    : 0;
  let maxConsecWins = 0, maxConsecLoss = 0, cw = 0, cl = 0;
  for (const t of closed) {
    if (_n(t.pnl) > 0) { cw++; cl = 0; } else { cl++; cw = 0; }
    if (cw > maxConsecWins) maxConsecWins = cw;
    if (cl > maxConsecLoss) maxConsecLoss = cl;
  }
  return {
    totalTrades: closed.length, wins: wins.length, losses: losses.length,
    winRate: _r2(winRate), totalPnl: _r2(totalPnl),
    grossWin: _r2(grossWin), grossLoss: _r2(grossLoss),
    avgWin: _r2(avgWin), avgLoss: _r2(avgLoss),
    rrRatio: _r2(rr), profitFactor: _r2(Math.min(profitFactor, 99.99)),
    maxDrawdown: _r2(maxDD), avgHoldMin: _r2(avgHoldMin),
    maxConsecWins, maxConsecLoss,
  };
}

app.get('/api/analytics/overview', (req, res) => {
  try {
    const trades     = db.data.trades     || [];
    const signals    = db.data.signals    || [];
    const rejections = db.data.rejections || [];

    const overall = _computeMetrics(trades);

    // Per-strategy breakdown
    const strategies = {};
    for (const t of trades) {
      const s = t.scanner || t.strategy || 'UNKNOWN';
      if (!strategies[s]) strategies[s] = [];
      strategies[s].push(t);
    }
    const byStrategy = Object.entries(strategies).map(([name, ts]) => ({
      name, ...(_computeMetrics(ts) || {}), tradeCount: ts.length,
    })).sort((a,b) => (_n(b.totalPnl)) - (_n(a.totalPnl)));

    // Daily P&L
    const pnlByDay = {};
    for (const t of trades.filter(t => t.status==='CLOSED' && t.pnl!=null)) {
      const d = _dateStr(t.closed_at || t.ts || Date.now());
      pnlByDay[d] = _r2((_pnlByDay_val => _pnlByDay_val)(pnlByDay[d] || 0) + _n(t.pnl));
    }
    const dailyPnl = Object.entries(pnlByDay)
      .sort(([a],[b]) => a.localeCompare(b))
      .slice(-60)
      .map(([date, pnl]) => ({ date, pnl }));

    // Equity curve
    let equity = 0;
    const equityCurve = dailyPnl.map(({ date, pnl }) => {
      equity += pnl; return { date, equity: _r2(equity) };
    });

    // Weekly P&L
    const pnlByWeek = {};
    for (const { date, pnl } of dailyPnl) {
      const d = new Date(date); const ws = new Date(d);
      ws.setDate(d.getDate() - d.getDay());
      const wk = ws.toISOString().slice(0,10);
      pnlByWeek[wk] = _r2((pnlByWeek[wk] || 0) + pnl);
    }
    const weeklyPnl = Object.entries(pnlByWeek)
      .sort(([a],[b]) => a.localeCompare(b)).slice(-12)
      .map(([week,pnl]) => ({ week, pnl }));

    // Monthly P&L
    const pnlByMonth = {};
    for (const { date, pnl } of dailyPnl) {
      const mo = date.slice(0,7);
      pnlByMonth[mo] = _r2((pnlByMonth[mo]||0) + pnl);
    }
    const monthlyPnl = Object.entries(pnlByMonth)
      .sort(([a],[b]) => a.localeCompare(b))
      .map(([month,pnl]) => ({ month, pnl }));

    // By hour
    const byHour = Array.from({length:24},(_,i) => ({hour:i,wins:0,losses:0,pnl:0}));
    for (const t of trades.filter(t => t.status==='CLOSED' && (t.opened_at||t.ts))) {
      const h = new Date(t.opened_at||t.ts).getUTCHours();
      if (_n(t.pnl)>0) byHour[h].wins++; else byHour[h].losses++;
      byHour[h].pnl = _r2(byHour[h].pnl + _n(t.pnl));
    }

    // Best/worst
    const closed = trades.filter(t => t.status==='CLOSED' && t.pnl!=null);
    const bestTrades  = [...closed].sort((a,b)=>_n(b.pnl)-_n(a.pnl)).slice(0,5);
    const worstTrades = [...closed].sort((a,b)=>_n(a.pnl)-_n(b.pnl)).slice(0,5);

    // Readiness score
    let readiness = 0; const msgs = [];
    if (overall) {
      if (overall.totalTrades >= 50) readiness+=20; else msgs.push(`Need ${50-overall.totalTrades} more trades for statistical confidence`);
      if (overall.winRate >= 50)     readiness+=20; else msgs.push(`Win rate ${overall.winRate}% � target =50%`);
      if (overall.rrRatio >= 1.5)    readiness+=20; else msgs.push(`R:R ratio ${overall.rrRatio} � target =1.5`);
      if (overall.profitFactor>=1.5) readiness+=20; else msgs.push(`Profit factor ${overall.profitFactor} � target =1.5`);
      if (overall.maxDrawdown < overall.totalPnl*0.2) readiness+=20; else msgs.push(`Max drawdown too high vs total profit`);
    } else { msgs.push('No closed trades yet'); }

    // Today / week P&L
    const todayStr = _dateStr(Date.now());
    const todayPnl = _r2(trades.filter(t=>t.status==='CLOSED'&&_dateStr(t.closed_at||t.ts||0)===todayStr).reduce((s,t)=>s+_n(t.pnl),0));
    const weekAgo  = new Date(Date.now()-7*86400000);
    const weekPnl  = _r2(trades.filter(t=>t.status==='CLOSED'&&new Date(t.closed_at||t.ts||0)>=weekAgo).reduce((s,t)=>s+_n(t.pnl),0));

    const totalSignals  = signals.length;
    const totalRejected = rejections.length;
    const signalAcceptRate = totalSignals ? _r2(((totalSignals-totalRejected)/totalSignals)*100) : 0;

    res.json({ overall, byStrategy, dailyPnl, weeklyPnl, monthlyPnl, equityCurve, byHour, bestTrades, worstTrades, readiness, readinessMessages:msgs, signalAcceptRate, totalSignals, totalRejected, todayPnl, weekPnl });
  } catch(err) {
    console.error('Analytics overview error:', err);
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/analytics/daily-limit-check', (req, res) => {
  try {
    const dailyLossLimit = _n(req.query.limit || 200);
    const todayStr = _dateStr(Date.now());
    const todayPnl = (db.data.trades||[])
      .filter(t=>t.status==='CLOSED'&&_dateStr(t.closed_at||t.ts||0)===todayStr)
      .reduce((s,t)=>s+_n(t.pnl),0);
    res.json({ todayPnl:_r2(todayPnl), limitHit:todayPnl<=-dailyLossLimit, dailyLossLimit });
  } catch(err) { res.status(500).json({ error: err.message }); }
});

// ------------------------------------------------------------
// END ANALYTICS ROUTES v2.0
// ------------------------------------------------------------


attachScoring(app, db, {
  adminOnly,
  serviceOrAdmin,
  now,
  fmpKey: process.env.FMP_API_KEY,
  httpGet: async (url) => {
    const r = await fetch(url);
    return await r.json();
  }
});

// ── Kill switch endpoints ────────────────────────────────────
// Shared deal-id resolver. Only indices resolved POSITION ids correctly;
// building it once server-side means the other five need a one-node
// change rather than five bespoke ports of the same logic.
attachBrokerResolve(app, { scannerAuth, capitalLogin, invalidateCapitalSession, journalEvent });

// Proving-window scoreboard. Reuses the connectivity cache so a scanner
// whose params are NO_RUNTIME_READ says so on its own row -- its results
// cannot be attributed to any parameter set.
attachScoreboard(app, { adminOnly, computeParamConnectivity });

// Correcting account_size drops equity without a trade occurring, so the
// endpoint refuses to let that masquerade as a drawdown.
attachAccountSize(app, { adminOnly, db, save, sendTelegramAlert, logChangelogRow });

attachParamStore(app, db, { adminOnly, journalEvent, sendTelegramAlert });

// Which dials actually reach a scanner? Derived from EXECUTED n8n run
// data, never from workflow source -- a param present in source but
// absent from run data is exactly what this exists to catch. Scanning
// the 1.6 GB sqlite is slow, so ?refresh=1 is opt-in.
app.get('/api/params/connectivity', adminOnly, async (req, res) => {
  try {
    const data = await computeParamConnectivity({ force: req.query.refresh === '1' });
    res.json({ ...data, cache_age_s: connectivityCacheAge() });
  } catch (e) { res.status(500).json({ error: e.message }); }
});
attachEventJournal(app, { adminOnly });
attachLayer1(app, db, { adminOnly, fmpProxyFetch,
  // Injected as a getter, not a value: the pause is armed long after this
  // line runs, and refreshFmpBackfillPause re-reads it from disk so a pause
  // written by another process is seen too.
  backfillPausedUntil: () => { refreshFmpBackfillPause(); return fmpBackfillPausedUntil; } });
attachLayer2(app, db, { adminOnly });
attachLayer4(app, db, { adminOnly, fetchFmpRegime: fetchFmpRegimeBundle });
attachLayer5(app, db, { adminOnly, sendTelegramAlert });
attachEarningsGuard(app, { scannerAuth, adminOnly, fmpProxyFetch, sendTelegramAlert });
attachExperiments(app, { adminOnly, getConfigHash, globalConfig:()=>db.data.scanner_config||{}, validateParamValue });
attachPositionWatchdog(app, { adminOnly, db, sendTelegramAlert, save });
app.post('/api/admin/financing/run',adminOnly,async(_req,res)=>{try{const result=await runFinancing(db);if(result.rows.length)await save();res.json(result);}catch(e){res.status(500).json({error:e.message});}});

function eventMs(row) {
  const value = row?.ts || row?.rejected_time || row?.created_at || row?.iso;
  const ms = typeof value === 'number' ? value : Date.parse(value || '');
  return Number.isFinite(ms) ? ms : 0;
}

function berlinClockParts(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/Berlin',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  }).formatToParts(date);
  return Object.fromEntries(parts.map(p => [p.type, p.value]));
}

function isBerlinMarketHours(date = new Date()) {
  const parts = berlinClockParts(date);
  const hour = Number(parts.hour);
  return !['Sat', 'Sun'].includes(parts.weekday) && hour >= 8 && hour < 21;
}

function fleetActivitySince(cutoffMs) {
  const signalCount = (db.data.signals || []).filter(row => eventMs(row) >= cutoffMs).length;
  const rejectionCount = (db.data.rejections || []).filter(row => eventMs(row) >= cutoffMs).length;
  const heartbeatCount = Object.values(db.data.heartbeats || {})
    .filter(row => eventMs(row) >= cutoffMs || Number(row?.ts || 0) >= cutoffMs).length;
  return { signals: signalCount, rejections: rejectionCount, heartbeats: heartbeatCount, total: signalCount + rejectionCount + heartbeatCount };
}

function cryptoActivitySince(cutoffMs) {
  const isCrypto = row => String(row?.scanner || row?.scanner_name || row?.data?.scanner || '').toLowerCase() === 'crypto';
  const signals = (db.data.signals || []).filter(row => isCrypto(row) && eventMs(row) >= cutoffMs).length;
  const rejections = (db.data.rejections || []).filter(row => isCrypto(row) && eventMs(row) >= cutoffMs).length;
  const heartbeat = db.data.heartbeats?.crypto;
  const heartbeats = heartbeat && eventMs(heartbeat) >= cutoffMs ? 1 : 0;
  return { signals, rejections, heartbeats, total: signals + rejections + heartbeats };
}

let lastDeadmanAlertBucket = '';
let lastHealthSignature = '';
let healthPushPrimed = false;

async function checkFleetDeadman({ simulate = false } = {}) {
  const marketHours = isBerlinMarketHours();
  const windowMinutes = marketHours ? 30 : 60;
  const cutoffMs = Date.now() - windowMinutes * 60_000;
  const activity = simulate ? { signals: 0, rejections: 0, heartbeats: 0, total: 0 } : (marketHours ? fleetActivitySince(cutoffMs) : cryptoActivitySince(cutoffMs));
  if (activity.total > 0) return { ok: true, sent: false, activity };
  const bucket = `${marketHours ? 'fleet' : 'crypto'}:${new Date(Math.floor(Date.now() / (windowMinutes * 60_000)) * windowMinutes * 60_000).toISOString()}`;
  if (!simulate && lastDeadmanAlertBucket === bucket) {
    return { ok: true, sent: false, skipped: 'already alerted this silent bucket', activity };
  }
  const clock = berlinClockParts();
  const message = `${simulate ? '[TEST] ' : ''}${marketHours ? 'FLEET' : 'CRYPTO'} SILENT: zero signals + rejections + heartbeats in the last ${windowMinutes} minutes. Berlin ${clock.weekday} ${clock.hour}:${clock.minute}.`;
  const telegram = await sendTelegramAlert(message);
  if (!simulate) lastDeadmanAlertBucket = bucket;
  return { ok: telegram.ok, sent: true, activity, message, telegram };
}

async function checkHealthChange({ simulate = false } = {}) {
  const payload = await buildHealthPayload();
  const signature = JSON.stringify({
    status: simulate ? `test-${payload.status}` : payload.status,
    issues: [...(payload.issues || [])].sort()
  });
  if (!simulate && !healthPushPrimed) {
    lastHealthSignature = signature;
    healthPushPrimed = true;
    return { ok: true, sent: false, primed: true, health: payload };
  }
  if (!simulate && signature === lastHealthSignature) return { ok: true, sent: false, unchanged: true, health: payload };
  const previous = lastHealthSignature || '(none)';
  const message = `${simulate ? '[TEST] ' : ''}Dashboard health changed: status=${payload.status}; issues=${payload.issues.join(',') || 'none'}; previous=${previous}`;
  const telegram = await sendTelegramAlert(message);
  if (!simulate) lastHealthSignature = signature;
  return { ok: telegram.ok, sent: true, message, health: payload, telegram };
}

let lastDigestLondonDay = '';
function londonClockParts(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', hour12: false }).formatToParts(date);
  return Object.fromEntries(parts.map(p => [p.type, p.value]));
}

async function runDailyDigest({ force = false } = {}) {
  const clock = londonClockParts();
  const day = `${clock.year}-${clock.month}-${clock.day}`;
  if (!force && (clock.hour !== '07' || lastDigestLondonDay === day)) return { ok: true, skipped: true };
  const brain = await brainShadowStats();
  const vetoes = Number(brain.overall?.total_shadow_vetoes ?? brain.shadow_vetoes ?? brain.vetoes ?? brain.total ?? 0);
  const labeled = Number(brain.overall?.labeled ?? brain.labeled ?? brain.labeled_vetoes ?? 0);
  const crypto = cryptoActivitySince(Date.now() - 12 * 60 * 60_000);
  const fleet = fleetActivitySince(Date.now() - 60 * 60_000);
  const budget = fmpBudgetSnapshot();
  const health = await buildHealthPayload();
  const newest = (db.data.filter_verdicts || [])[0] || null;
  const financing = await financingDigestLines();
  const scannerErrors = await scannerErrorStats().catch(() => ({ total: 0, by_scanner_stage: [] }));
  const scannerErrorLines = scannerErrors.total
    ? scannerErrors.by_scanner_stage.map(r => `  ${r.scanner}/${r.stage} (${r.error_class}): ${r.n}`)
    : ['  none'];
  const margin = db.data.binance_margin_status;
  const priority = margin && Number(margin.margin_level) < 1.5 ? `CRITICAL: Binance cross-margin ${Number(margin.margin_level).toFixed(3)}` : null;
  // Per-scanner block. buildScoreboard only reads, and scannerDigestLines is
  // pure, so this adds no side effect to the digest.
  let scannerLines;
  try {
    scannerLines = scannerDigestLines(await buildScoreboard({ days: 7 }));
  } catch (e) {
    scannerLines = [`  (scoreboard unavailable: ${e.message})`];
  }
  const message = [
    ...(priority ? [priority] : []),
    `PRIORITY: brain shadow — vetoes ${vetoes}, labeled ${labeled}, days-to-review ${Math.max(0, 30 - labeled)}`,
    `Fleet 60m: ${fleet.total} events; overnight crypto: ${crypto.total}`,
    `FMP budget runway: ${budget.backfill_remaining}/${budget.backfill_daily_cap}`,
    `Newest verdict/evaluation: ${newest ? `${newest.scanner || '?'} ${newest.verdict || newest.reason || '?'}` : 'none'}`,
    `Health: ${health.status}${health.issues?.length ? ` — ${health.issues.join(', ')}` : ''}`,
    `Scanner errors (24h): ${scannerErrors.total}`,
    ...scannerErrorLines,
    'Per scanner (7d):',
    ...scannerLines,
    ...financing
  ].join('\n');
  const telegram = await sendTelegramAlert(message);
  if (telegram.ok) lastDigestLondonDay = day;
  return { ok: telegram.ok, day, message, telegram };
}

app.post('/api/admin/digest/run', adminOnly, async (_req, res) => {
  try { res.json(await runDailyDigest({ force: true })); } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/api/admin/risk/reset-loss-halt', adminOnly, async (req,res) => {
  const scanner=String(req.body?.scanner||'').toLowerCase();
  if (!scanner) return res.status(400).json({error:'scanner required'});
  db.data.scanner_config.loss_halt_reset_at ||= {};
  db.data.scanner_config.loss_halt_reset_at[scanner]=now();
  await save();
  res.json({status:'ok',scanner,reset_at:db.data.scanner_config.loss_halt_reset_at[scanner]});
});
app.post('/api/admin/risk/breakers/test', adminOnly, (req,res) => {
  const b=req.body||{}, maxOrders=Number(b.max_orders??3), maxLossR=Number(b.max_daily_loss_r??3), maxConsecutive=Number(b.consecutive_loss_halt??5);
  let reason=null;
  if(Number(b.orders_today||0)>=maxOrders) reason=`MAX_ORDERS_PER_DAY: ${b.orders_today}/${maxOrders}`;
  else if(Number(b.daily_loss_r||0)>=maxLossR) reason=`MAX_DAILY_LOSS_R: ${Number(b.daily_loss_r).toFixed(2)}/${maxLossR}`;
  else if(Number(b.consecutive_losses||0)>=maxConsecutive) reason=`CONSECUTIVE_LOSS_HALT: ${b.consecutive_losses}/${maxConsecutive}; manual reset required`;
  res.json({allowed:!reason,reason,simulation:true});
});

app.post('/api/admin/alerts/deadman/test', adminOnly, async (req, res) => {
  try {
    res.json(await checkFleetDeadman({ simulate: true }));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/admin/alerts/health-change/test', adminOnly, async (req, res) => {
  try {
    res.json(await checkHealthChange({ simulate: true }));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

function startFleetMonitors() {
  const deadmanTimer = setInterval(() => {
    checkFleetDeadman().catch(e => console.error('deadman monitor failed', e));
  }, 15 * 60_000);
  const healthTimer = setInterval(() => {
    checkHealthChange().catch(e => console.error('health-change monitor failed', e));
  }, 60_000);
  const digestTimer = setInterval(() => {
    runDailyDigest().catch(e => console.error('daily digest failed', e));
  }, 60_000);
  deadmanTimer.unref?.();
  healthTimer.unref?.();
  digestTimer.unref?.();
  checkHealthChange().catch(e => console.error('health-change monitor prime failed', e));
}

app.get('/api/admin/kill-switch', adminOnly, (req, res) => {
  const cfg = db.data.scanner_config || {};
  res.json({
    kill_switch: cfg.kill_switch === true,
    paper_only: cfg.paper_only === true,
    updated_at: cfg.updated_at || null,
    updated_by: cfg.halt_updated_by || null,
    reason: cfg.halt_reason || null
  });
});
app.post('/api/admin/kill-switch', adminOnly, async (req, res) => {
  const { active, paper_only, reason } = req.body || {};
  // Leaving a halt must be at least as deliberate as entering one, so the
  // reason is required in BOTH directions and for either field.
  const why = String(reason || '').trim();
  if (!why) return res.status(400).json({ error: 'reason required for any kill-switch or paper_only change' });
  if (active === undefined && paper_only === undefined) {
    return res.status(400).json({ error: 'nothing to change: supply active and/or paper_only' });
  }
  db.data.scanner_config = db.data.scanner_config || {};
  const cfg = db.data.scanner_config;
  const who = req.investorId || 'admin';
  // Real previous values, captured before mutation -- never a literal.
  const prevKill = cfg.kill_switch === true;
  const prevPaper = cfg.paper_only === true;

  if (active !== undefined) cfg.kill_switch = active === true;
  if (paper_only !== undefined) cfg.paper_only = paper_only === true;
  cfg.updated_at = now();
  cfg.halt_updated_by = who;
  cfg.halt_reason = why;
  await save();

  const transitions = [];
  if (active !== undefined && cfg.kill_switch !== prevKill) transitions.push(['kill_switch', prevKill, cfg.kill_switch]);
  if (paper_only !== undefined && cfg.paper_only !== prevPaper) transitions.push(['paper_only', prevPaper, cfg.paper_only]);

  for (const [field, from, to] of transitions) {
    // BOTH directions. Turning the fleet back ON previously left no record
    // at all, which made the more dangerous action the unlogged one.
    await journalEvent(field === 'kill_switch' ? (to ? 'halt' : 'resume') : 'paper_only_change', {
      scanner: 'all',
      payload: { field, old_value: from, new_value: to, reason: why, changed_by: who }
    }).catch(() => {});
    await logChangelogRow({
      scanner: 'all', parameter: field, old_value: from, new_value: to,
      reason: why, approved_by: `admin:${who}`
    });
    sendTelegramAlert(
      `${field === 'kill_switch' ? (to ? 'FLEET HALTED' : 'FLEET RESUMED') : 'PAPER_ONLY ' + (to ? 'ON' : 'OFF')}\n` +
      `field: ${field}\nfrom: ${from}\nto: ${to}\nby: ${who}\nreason: ${why}`
    ).catch(() => {});
  }

  res.json({
    status: 'ok', kill_switch: cfg.kill_switch, paper_only: cfg.paper_only,
    transitions: transitions.map(([f, from, to]) => ({ field: f, from, to })),
    updated_by: who, reason: why
  });
});

// ── Request timeout ──────────────────────────────────────────
app.use((req, res, next) => {
  req.setTimeout(30000, () => {
    res.status(503).json({ error: 'Request timeout' });
  });
  res.setTimeout(30000);
  next();
});

app.get('*', (req,res) => res.sendFile(path.join(__dirname,'../public/index.html')));

// ── Error handling middleware ────────────────────────────────
app.use((err, req, res, next) => {
  console.error('Unhandled error:', err.stack || err.message || err);
  res.status(err.status || 500).json({ error: err.message || 'Internal server error' });
});

app.listen(PORT, () => {
  console.log(`\n🏦 Fund Management System → http://localhost:${PORT}`);
  console.log(`   Admin:    http://localhost:${PORT}`);
  console.log(`   Investor: http://localhost:${PORT}/investor\n`);
  if (!SCANNER_API_KEY) console.warn('WARNING: SCANNER_API_KEY is unset; scanner write endpoints are not authenticated.');
  if (!corsOrigins.length) console.warn('WARNING: CORS_ORIGINS is unset; cross-origin requests are unrestricted.');
  if (!process.env.ADMIN_PIN || process.env.ADMIN_PIN === '1234') console.warn('WARNING: default ADMIN_PIN is active.');
  startFleetMonitors();
  startLayer3Jobs(db, { sendTelegramAlert, save });
  startLayer4Jobs({ fetchFmpRegime: fetchFmpRegimeBundle });
  startLayer5Jobs({ sendTelegramAlert });
  startEarningsGuardJobs({ fmpProxyFetch });
  startExperimentJobs();
  startPositionWatchdogs(db, { sendTelegramAlert, save, journalEvent });
  startFinancingJob(db, { save });
  startLatencyMonitor({ sendTelegramAlert });
});
