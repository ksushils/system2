import express from 'express';
import cors from 'cors';
import crypto from 'crypto';
import path from 'path';
import { fileURLToPath } from 'url';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';
import { JSONFilePreset } from 'lowdb/node';
import attachScoring from './scoring-endpoints.cjs';
import {
  attachParamStore,
  getScannerParams,
  initParamStore
} from './param-store.js';
import {
  applyTradePricePath,
  attachLayer1,
  computeFillSlippage,
  finalizeTradePath,
  initLayer1
} from './layer1.js';
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
  insertBrainPostgres
} from './storage-adapter.js';
import fundIntegrity from './fund-integrity.cjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app  = express();
const PORT = process.env.PORT || 3210;

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
// Postgres write failures are caught inside saveToPostgres so they never crash a request;
// the in-memory data stays intact and the JSON file remains a fallback.
const save = async () => {
  if (postgresEnabled) {
    await saveToPostgres(db.data);
    if (dualWriteEnabled) { try { await db.write(); } catch(e){ console.error('dual-write json failed:', e.message); } }
  } else {
    await db.write();
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
  const adminPin = process.env.ADMIN_PIN || '1234';
  if (hashPin(pin) !== hashPin(adminPin)) return res.status(401).json({ error:'Wrong PIN' });
  const token = mkToken();
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
    const signal = { id:nid('signals'), ts:b.ts||now(), scanner:b.scanner||'unknown', type:b.type||'skip', ticker:(b.ticker||b.pair||b.asset).toUpperCase(), detail:b.detail||'', entry:b.entry||b.entry_price||null, sl:b.sl||b.stop_loss||null, tp:b.tp||b.take_profit_1||null, quality:b.quality_score||null, adx:b.adx||null, rsi:b.rsi||null, volume_ratio:b.volume_ratio||null };
    db.data.signals.unshift(signal);
    if(db.data.signals.length>1000) db.data.signals=db.data.signals.slice(0,1000);
    await saveSignalHot(signal); res.json({status:'ok'});
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
    const dealId = b.deal_id || b.dealId || null;
    if (dealId && dealId !== 'UNKNOWN') {
      const existing = (db.data.trades || []).find(t => t.deal_id === dealId && ['OPEN','PARTIAL'].includes(t.status));
      if (existing) return res.json({status:'ok', duplicate:true, trade_id:existing.id});
    }
    const entry = b.entry ?? b.entry_price ?? null;
    const intendedEntry = b.intended_entry ?? b.intendedEntry ?? null;
    const initialSl = b.sl ?? b.stop_loss ?? null;
    const trade = { id:nid('trades'), ts:b.ts||now(), scanner:b.scanner||'unknown', ticker:(b.ticker||b.epic).toUpperCase(), deal_id:b.deal_id||b.dealId||null, direction:String(b.direction).toUpperCase(), setup_type:b.setup_type||b.type||'', entry, intended_entry:intendedEntry, fill_slippage_pct:computeFillSlippage(b.direction, entry, intendedEntry), sl:initialSl, initial_sl:initialSl, tp1:b.tp1||b.take_profit_1||null, tp2:b.tp2||b.take_profit_2||null, size:b.size||null, risk_usd:b.risk_usd||null, status:'OPEN', close_price:null, pnl:null, max_favorable:entry, max_adverse:entry, mae_r:null, mfe_r:null, opened_at:b.ts||now(), closed_at:null };
    db.data.trades.unshift(trade);
    await saveTradeHot(trade); res.json({status:'ok'});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/trade/close', scannerAuth, async (req,res)=>{
  try {
    const b=req.body;
    const t=(db.data.trades||[]).find(t=>(b.deal_id&&t.deal_id===b.deal_id)||(b.ticker&&t.ticker===b.ticker&&['OPEN','PARTIAL'].includes(t.status)));
    if(t){
      const closePrice = b.close_price??b.closePrice??b.current_price??null;
      applyTradePricePath(t, closePrice);
      t.status=b.action==='PARTIAL_EXIT'?'PARTIAL':'CLOSED';
      t.close_price=closePrice;
      t.pnl=b.pnl??b.pnl_realised??null;
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
    await saveUpdateHot(update);
    res.json({status:'ok',found:!!t});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/trade/update', scannerAuth, async (req,res)=>{
  try {
    const b=req.body;
    const t=(db.data.trades||[]).find(t=>(b.deal_id&&t.deal_id===b.deal_id)||(b.ticker&&t.ticker===b.ticker&&['OPEN','PARTIAL'].includes(t.status)));
    if(t&&b.new_sl) t.sl=b.new_sl;
    if(t) applyTradePricePath(t, b.current_price);
    const update = { id:nid('updates'), ts:b.ts||now(), deal_id:b.deal_id||null, ticker:b.ticker||null, scanner:b.scanner||null, action:'UPDATE_SL', old_sl:b.old_sl||null, new_sl:b.new_sl||null, current_price:b.current_price??null };
    db.data.updates.unshift(update);
    if (t) await saveTradeHot(t);
    await saveUpdateHot(update);
    res.json({status:'ok',found:!!t});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/rejection', scannerAuth, async (req,res)=>{
  try {
    const b=req.body;
    const rejection = {
      ...b,
      id:nid('rejections'),
      ts:b.ts||now(),
      scanner:b.scanner||'unknown',
      ticker:b.ticker||'UNKNOWN',
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
    };
    db.data.rejections.unshift(rejection);
    if(db.data.rejections.length>500) db.data.rejections=db.data.rejections.slice(0,500);
    await saveRejectionHot(rejection); res.json({status:'ok'});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
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
    positions.forEach(pos => {
      const dealId   = pos.dealId || pos.deal_id || '';
      const ticker   = (pos.epic || pos.ticker || '').toUpperCase();
      const pnl      = parseFloat(pos.pnl || pos.profit || 0);
      const closePrice = parseFloat(pos.closeLevel || pos.close_price || 0);
      const closedAt   = pos.createdDateUtc || pos.closed_at || now();
      // Match by dealId first, then ticker + open status
      let trade = (db.data.trades||[]).find(t =>
        (dealId && t.deal_id === dealId) ||
        (ticker && t.ticker === ticker && ['OPEN','PARTIAL'].includes(t.status))
      );
      if (trade) {
        trade.status      = 'CLOSED';
        trade.pnl         = pnl;
        trade.close_price = closePrice;
        trade.closed_at   = closedAt;
        updated++;
      } else { unmatched++; }
    });
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
    if (pnl       != null) trade.pnl         = parseFloat(pnl);
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
      const h = new Date(t.ts).toLocaleString('en-US',{timeZone:'America/New_York',hour:'numeric',hour12:false});
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
let fmpProcessing = false;
let fmpBackoffUntil = 0;
let fmpBackfillPausedUntil = Date.parse(process.env.FMP_BACKFILL_PAUSED_UNTIL || readLocalEnvValue('FMP_BACKFILL_PAUSED_UNTIL') || '') || 0;
const FMP_BUDGET_PATH = process.env.FMP_BUDGET_PATH || readLocalEnvValue('FMP_BUDGET_PATH') || path.join(DATA_DIR, 'fmp-proxy-budget.json');
const FMP_BACKFILL_DAILY_CAP = Number(process.env.FMP_BACKFILL_DAILY_CAP || readLocalEnvValue('FMP_BACKFILL_DAILY_CAP') || 1500);
const FMP_MIN_INTERVAL_MS = Math.ceil(60_000 / 250);
const FMP_STALE_MAX_MS = 15 * 60_000;
const FMP_QUOTE_TTL_MS = 3 * 60_000;

function fmpDayKey(ts = new Date()) {
  return ts.toISOString().slice(0, 10);
}

function loadFmpBudget() {
  try {
    if (!existsSync(FMP_BUDGET_PATH)) return { day: fmpDayKey(), outbound: 0, backfill_outbound: 0, live_outbound: 0, rate_limited: 0, reset_observed_at: null };
    const parsed = JSON.parse(readFileSync(FMP_BUDGET_PATH, 'utf8'));
    if (parsed.day !== fmpDayKey()) return { day: fmpDayKey(), outbound: 0, backfill_outbound: 0, live_outbound: 0, rate_limited: 0, reset_observed_at: parsed.reset_observed_at || null };
    return { outbound: 0, backfill_outbound: 0, live_outbound: 0, rate_limited: 0, reset_observed_at: null, ...parsed };
  } catch (e) {
    console.error('[fmp-budget] load failed:', e.message);
    return { day: fmpDayKey(), outbound: 0, backfill_outbound: 0, live_outbound: 0, rate_limited: 0, reset_observed_at: null };
  }
}

let fmpBudget = loadFmpBudget();

function persistFmpBudget() {
  try {
    mkdirSync(path.dirname(FMP_BUDGET_PATH), { recursive: true });
    writeFileSync(FMP_BUDGET_PATH, JSON.stringify(fmpBudget, null, 2));
  } catch (e) {
    console.error('[fmp-budget] persist failed:', e.message);
  }
}

function refreshFmpBudgetDay() {
  const day = fmpDayKey();
  if (fmpBudget.day !== day) {
    fmpBudget = { day, outbound: 0, backfill_outbound: 0, live_outbound: 0, rate_limited: 0, reset_observed_at: fmpBudget.reset_observed_at || null };
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

function queueFmp(url, traffic = 'live') {
  return new Promise((resolve, reject) => {
    fmpQueue.push({ url, traffic, resolve, reject });
    runFmpQueue();
  });
}

async function fmpProxyFetch(cacheKey, ttlMs, endpoint) {
  fmpStats.requests++;
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
  const result = await queueFmp(url, isBackfill ? 'backfill' : 'live');
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

function sendFmpProxy(res, result) {
  res.set('X-FMP-Proxy-Cache', result.cache);
  for (const [k, v] of Object.entries(result.headers || {})) res.set(`X-FMP-${k}`, String(v));
  res.status(result.status || 200).json(result.body);
}

app.get('/api/proxy/fmp/quote', scannerAuth, async (req, res) => {
  try {
    const ticker = String(req.query.ticker || '').trim().toUpperCase();
    if (!/^[A-Z0-9.^-]{1,20}$/.test(ticker)) return res.status(400).json({ error: 'valid ticker required' });
    const result = await fmpProxyFetch(`quote:${ticker}`, FMP_QUOTE_TTL_MS, `stable/quote?symbol=${encodeURIComponent(ticker)}`);
    sendFmpProxy(res, result);
  } catch (e) {
    res.status(e.statusCode || 500).json({ error: e.message });
  }
});

app.get('/api/proxy/fmp/candles', scannerAuth, async (req, res) => {
  try {
    const ticker = String(req.query.ticker || '').trim().toUpperCase();
    const resKey = String(req.query.res || '5min').trim().toLowerCase();
    const allowed = new Set(['1min', '5min', '15min', '30min', '1hour', '4hour']);
    if (!/^[A-Z0-9.^-]{1,20}$/.test(ticker)) return res.status(400).json({ error: 'valid ticker required' });
    if (!allowed.has(resKey)) return res.status(400).json({ error: 'unsupported res; use 1min,5min,15min,30min,1hour,4hour' });
    const endpoint = `stable/historical-chart/${encodeURIComponent(resKey)}?symbol=${encodeURIComponent(ticker)}`;
    const result = await fmpProxyFetch(`candles:${ticker}:${resKey}`, 5 * 60_000, endpoint);
    sendFmpProxy(res, result);
  } catch (e) {
    res.status(e.statusCode || 500).json({ error: e.message });
  }
});

app.get('/api/proxy/fmp/regime', scannerAuth, async (req, res) => {
  try {
    const cached = fmpCacheGet('regime');
    if (cached) {
      res.set('X-FMP-Proxy-Cache', 'HIT');
      return res.json(cached.body);
    }
    fmpStats.requests++;
    fmpStats.misses++;
    const [spy, vix] = await Promise.all([
      fmpProxyFetch('quote:SPY', FMP_QUOTE_TTL_MS, 'stable/quote?symbol=SPY'),
      fmpProxyFetch('quote:%5EVIX', FMP_QUOTE_TTL_MS, 'stable/quote?symbol=%5EVIX')
    ]);
    const body = { spy: Array.isArray(spy.body) ? spy.body[0] : spy.body, vix: Array.isArray(vix.body) ? vix.body[0] : vix.body };
    fmpCacheSet('regime', 10 * 60_000, { status: 200, ok: true, headers: { ...spy.headers, ...vix.headers }, body });
    res.set('X-FMP-Proxy-Cache', 'MISS');
    res.json(body);
  } catch (e) {
    res.status(e.statusCode || 500).json({ error: e.message });
  }
});

app.get('/api/proxy/fmp/raw', scannerAuth, async (req, res) => {
  try {
    let rawPath = String(req.query.path || '').trim();
    if (!rawPath) return res.status(400).json({ error: 'path required' });
    rawPath = rawPath.replace(/^\/+/, '');
    // FMP now rejects some legacy api/v3 paths upstream. Prefer stable/... paths.
    if (!/^(stable|api\/v3)\/[A-Za-z0-9_./-]+(\?.*)?$/.test(rawPath)) {
      return res.status(400).json({ error: 'path must be an FMP stable/ or api/v3 path; prefer stable/ paths' });
    }
    if (/apikey=/i.test(rawPath)) return res.status(400).json({ error: 'do not include apikey in path' });
    const result = await fmpProxyFetch(`raw:${rawPath}`, 5 * 60_000, rawPath);
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
    cache_hit_rate_pct: total ? +(fmpStats.hits / total * 100).toFixed(1) : 0,
    fmp_key_configured: !!fmpApiKey()
  });
});

// ════════════════════════════════════════════════════════════
// CENTRALIZED CONFIG — single source of truth for all scanners
// ════════════════════════════════════════════════════════════

// Scanners GET this at startup instead of hardcoding values.
// Public (no auth) so n8n can fetch without credentials.
app.get('/api/config', async (req, res) => {
  const base = { ...(db.data.scanner_config || {}) };
  const scanner = req.query.scanner ? String(req.query.scanner) : '';
  if (!scanner) return res.json(base);
  try {
    const merged = await getScannerParams(scanner);
    res.json({
      ...base,
      params: merged.params,
      params_updated_at: merged.params_updated_at,
      params_source: Object.keys(merged.params || {}).length ? 'server' : 'local'
    });
  } catch (e) {
    res.json({
      ...base,
      params: {},
      params_updated_at: null,
      params_source: 'local',
      params_error: e.message
    });
  }
});

// Admin updates config from dashboard Settings
app.post('/api/config', adminOnly, async (req, res) => {
  db.data.scanner_config = {
    ...db.data.scanner_config,
    ...req.body,
    updated_at: new Date().toISOString()
  };
  await save();
  res.json({ status: 'ok', config: db.data.scanner_config });
});

// ════════════════════════════════════════════════════════════
// GLOBAL RISK LEDGER — portfolio heat across ALL scanners
// ════════════════════════════════════════════════════════════

// Scanner calls this BEFORE placing an order. Returns allowed:true/false.
// Enforces: master kill switch, paper-only, max global heat %, max positions.
app.post('/api/risk/check', scannerAuth, async (req, res) => {
  const cfg = db.data.scanner_config || {};
  const { scanner, ticker, risk_amount = 0 } = req.body || {};

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
  const { scanner, ticker, deal_id, risk_amount = 0 } = req.body || {};
  if (!deal_id || deal_id === 'UNKNOWN') return res.status(400).json({ error: 'valid deal_id required' });
  db.data.risk_ledger = db.data.risk_ledger || [];
  // Avoid duplicate by deal_id
  db.data.risk_ledger = db.data.risk_ledger.filter(p => p.deal_id !== deal_id);
  db.data.risk_ledger.push({
    scanner, ticker, deal_id,
    risk_amount: Number(risk_amount),
    opened_at: new Date().toISOString()
  });
  await save();
  res.json({ status: 'ok', open_positions: db.data.risk_ledger.length });
});

// Scanner clears a closed position from the ledger
app.post('/api/risk/close', scannerAuth, async (req, res) => {
  const { deal_id } = req.body || {};
  if (!deal_id || deal_id === 'UNKNOWN') return res.status(400).json({ error: 'valid deal_id required' });
  db.data.risk_ledger = (db.data.risk_ledger || []).filter(p => p.deal_id !== deal_id);
  await save();
  res.json({ status: 'ok', open_positions: db.data.risk_ledger.length });
});

// Dashboard reads current global risk state
app.get('/api/risk-status', (req, res) => {
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

function buildHealthPayload() {
  const trades = db.data.trades || [];
  const tradeOpen = trades.filter(t => ['OPEN','PARTIAL'].includes(t.status)).length;
  const riskOpen = (db.data.risk_ledger || []).length;
  const liveOpen = Array.isArray(db.data.live_positions?.positions) ? db.data.live_positions.positions.length : 0;
  const heartbeatCount = Object.keys(db.data.heartbeats || {}).length;
  const issues = [];
  if (tradeOpen !== riskOpen) issues.push('trade_risk_position_mismatch');
  if (tradeOpen !== liveOpen) issues.push('trade_live_position_mismatch');
  if (heartbeatCount < 7) issues.push('missing_scanner_heartbeats');
  if (!SCANNER_API_KEY) issues.push('scanner_api_key_not_enforced');
  if (!corsOrigins.length) issues.push('cors_allowlist_not_configured');
  return {
    status: issues.length ? 'degraded' : 'ok',
    timestamp: now(),
    storage: postgresEnabled ? (dualWriteEnabled ? 'postgres_dual_write' : 'postgres') : 'json',
    counts: {
      trades: trades.length,
      open_trades: tradeOpen,
      risk_positions: riskOpen,
      live_positions: liveOpen,
      scanner_heartbeats: heartbeatCount
    },
    issues
  };
}

app.get('/api/health', (req, res) => {
  const payload = buildHealthPayload();
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

// Query the brain: given a new signal, how did similar past trades go?
app.post('/api/brain/similar', scannerAuth, (req, res) => {
  const signal = req.body || {};
  const qf = brainFeatures(signal);
  const brain = db.data.trade_brain || [];
  const MIN_SIM = Number(signal.min_similarity ?? 0.55);

  const scored = brain.map(r => ({ r, sim: brainSimilarity(qf, r.features) }))
    .filter(x => x.sim >= MIN_SIM)
    .sort((a, b) => b.sim - a.sim);

  const matches = scored.slice(0, 50);
  const n = matches.length;
  if (n === 0) {
    return res.json({
      samples: 0,
      verdict: 'NO_DATA',
      message: 'No similar past trades yet. Brain needs more closed trades to learn this setup.',
      query_features: qf
    });
  }
  const wins = matches.filter(m => m.r.outcome.win).length;
  const winRate = Math.round((wins / n) * 100);
  const rVals = matches.map(m => m.r.outcome.r_multiple).filter(v => v !== null && !isNaN(v));
  const avgR = rVals.length ? +(rVals.reduce((a, b) => a + b, 0) / rVals.length).toFixed(2) : null;
  const totalPnl = +matches.reduce((a, m) => a + (m.r.outcome.pnl || 0), 0).toFixed(2);

  // Verdict
  let verdict = 'NEUTRAL';
  if (n >= 5) {
    if (winRate >= 60 && (avgR === null || avgR > 0.3)) verdict = 'STRONG_POSITIVE';
    else if (winRate >= 52) verdict = 'POSITIVE';
    else if (winRate <= 40 || (avgR !== null && avgR < -0.2)) verdict = 'NEGATIVE';
  } else {
    verdict = 'LOW_CONFIDENCE';
  }

  res.json({
    samples: n,
    win_rate: winRate,
    avg_r: avgR,
    total_pnl: totalPnl,
    verdict,
    confidence: n >= 20 ? 'HIGH' : n >= 8 ? 'MEDIUM' : 'LOW',
    top_similarity: +(matches[0].sim).toFixed(2),
    message: `${n} similar setups: ${wins}W/${n - wins}L (${winRate}%)${avgR !== null ? `, avg ${avgR}R` : ''}`,
    examples: matches.slice(0, 5).map(m => ({
      ticker: m.r.ticker, setup: m.r.setup_type, direction: m.r.direction,
      win: m.r.outcome.win, r: m.r.outcome.r_multiple, pnl: m.r.outcome.pnl,
      similarity: +(m.sim).toFixed(2), when: m.r.recorded_at
    })),
    query_features: qf
  });
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
  now,
  fmpKey: process.env.FMP_API_KEY,
  httpGet: async (url) => {
    const r = await fetch(url);
    return await r.json();
  }
});

// ── Kill switch endpoints ────────────────────────────────────
attachParamStore(app, db, { adminOnly });
attachLayer1(app, db, { adminOnly, fmpProxyFetch });

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

let lastDeadmanAlertBucket = '';
let lastHealthSignature = '';
let healthPushPrimed = false;

async function checkFleetDeadman({ simulate = false } = {}) {
  if (!simulate && !isBerlinMarketHours()) return { ok: true, skipped: 'outside Berlin market-hours window' };
  const cutoffMs = Date.now() - 30 * 60_000;
  const activity = simulate ? { signals: 0, rejections: 0, heartbeats: 0, total: 0 } : fleetActivitySince(cutoffMs);
  if (activity.total > 0) return { ok: true, sent: false, activity };
  const bucket = new Date(Math.floor(Date.now() / (30 * 60_000)) * 30 * 60_000).toISOString();
  if (!simulate && lastDeadmanAlertBucket === bucket) {
    return { ok: true, sent: false, skipped: 'already alerted this silent bucket', activity };
  }
  const clock = berlinClockParts();
  const message = `${simulate ? '[TEST] ' : ''}FLEET SILENT: zero signals + rejections + heartbeats in the last 30 minutes. Berlin ${clock.weekday} ${clock.hour}:${clock.minute}.`;
  const telegram = await sendTelegramAlert(message);
  if (!simulate) lastDeadmanAlertBucket = bucket;
  return { ok: telegram.ok, sent: true, activity, message, telegram };
}

async function checkHealthChange({ simulate = false } = {}) {
  const payload = buildHealthPayload();
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
  deadmanTimer.unref?.();
  healthTimer.unref?.();
  checkHealthChange().catch(e => console.error('health-change monitor prime failed', e));
}

app.get('/api/admin/kill-switch', adminOnly, (req, res) => {
  const cfg = db.data.scanner_config || {};
  res.json({ kill_switch: cfg.kill_switch === true, paper_only: cfg.paper_only === true, updated_at: cfg.updated_at || null });
});
app.post('/api/admin/kill-switch', adminOnly, async (req, res) => {
  const { active, paper_only } = req.body || {};
  db.data.scanner_config = db.data.scanner_config || {};
  if (active !== undefined) db.data.scanner_config.kill_switch = active === true;
  if (paper_only !== undefined) db.data.scanner_config.paper_only = paper_only === true;
  db.data.scanner_config.updated_at = now();
  await save();
  res.json({ status: 'ok', kill_switch: db.data.scanner_config.kill_switch, paper_only: db.data.scanner_config.paper_only });
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
});
