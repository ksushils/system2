import express from 'express';
import cors from 'cors';
import crypto from 'crypto';
import path from 'path';
import { fileURLToPath } from 'url';
import { mkdirSync } from 'fs';
import { JSONFilePreset } from 'lowdb/node';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app  = express();
const PORT = process.env.PORT || 3210;

app.use(cors());
app.use(express.json({ limit: '10mb' }));
app.use(express.static(path.join(__dirname, '../public')));

// ── DB ───────────────────────────────────────────────────────
const DB_PATH = process.env.DB_PATH || path.join(__dirname, '../data/fund.json');
mkdirSync(path.dirname(DB_PATH), { recursive: true });

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
  sessions: []
};

const db = await JSONFilePreset(DB_PATH, defaultData);

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
const save   = () => db.write();

const hashPin  = pin => crypto.createHash('sha256').update(pin + 'fund_salt_v1').digest('hex');
const mkToken  = () => crypto.randomBytes(28).toString('hex');
const SESSION_TTL = 14 * 24 * 60 * 60 * 1000;

const SCANNERS = ['fmp', 'forex', 'comm', 'pa', 'all'];

// ── Auth middleware ───────────────────────────────────────────
function auth(req, res, next) {
  const token = req.headers['x-token'] || req.query.token;
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

app.post('/api/auth/admin', async (req,res)=>{
  const { pin } = req.body;
  const adminPin = process.env.ADMIN_PIN || '1234';
  if (hashPin(pin) !== hashPin(adminPin)) return res.status(401).json({ error:'Wrong PIN' });
  const token = mkToken();
  db.data.sessions.push({ token, investor_id:'admin', is_admin:true, expires:Date.now()+SESSION_TTL, ts:now() });
  await save(); res.json({ token, role:'admin', name:'Admin' });
});

app.post('/api/auth/investor', async (req,res)=>{
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
    scanners_enabled:['fmp','forex','comm','pa'],
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

app.post('/api/signal', async (req,res)=>{
  try {
    const b=req.body;
    db.data.signals.unshift({ id:nid('signals'), ts:b.ts||now(), scanner:b.scanner||'unknown', type:b.type||'skip', ticker:b.ticker||b.pair||b.asset||'UNKNOWN', detail:b.detail||'', entry:b.entry||b.entry_price||null, sl:b.sl||b.stop_loss||null, tp:b.tp||b.take_profit_1||null, quality:b.quality_score||null, adx:b.adx||null, rsi:b.rsi||null, volume_ratio:b.volume_ratio||null });
    if(db.data.signals.length>1000) db.data.signals=db.data.signals.slice(0,1000);
    await save(); res.json({status:'ok'});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/trade/open', async (req,res)=>{
  try {
    const b=req.body;
    db.data.trades.unshift({ id:nid('trades'), ts:b.ts||now(), scanner:b.scanner||'unknown', ticker:b.ticker||b.epic||'UNKNOWN', deal_id:b.deal_id||b.dealId||null, direction:b.direction||'LONG', setup_type:b.setup_type||b.type||'', entry:b.entry||b.entry_price||null, sl:b.sl||b.stop_loss||null, tp1:b.tp1||b.take_profit_1||null, tp2:b.tp2||b.take_profit_2||null, size:b.size||null, risk_usd:b.risk_usd||null, status:'OPEN', close_price:null, pnl:null, opened_at:b.ts||now(), closed_at:null });
    await save(); res.json({status:'ok'});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/trade/close', async (req,res)=>{
  try {
    const b=req.body;
    const t=(db.data.trades||[]).find(t=>(b.deal_id&&t.deal_id===b.deal_id)||(b.ticker&&t.ticker===b.ticker&&t.status==='OPEN'));
    if(t){ t.status=b.action==='PARTIAL_EXIT'?'PARTIAL':'CLOSED'; t.close_price=b.close_price||b.closePrice||null; t.pnl=b.pnl||b.pnl_realised||null; if(t.status==='CLOSED') t.closed_at=b.ts||now(); }
    db.data.updates.unshift({ id:nid('updates'), ts:b.ts||now(), deal_id:b.deal_id||null, ticker:b.ticker||null, scanner:b.scanner||null, action:b.action||'FULL_EXIT', close_price:b.close_price||null, pnl_realised:b.pnl||null });
    await save(); res.json({status:'ok',found:!!t});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/trade/update', async (req,res)=>{
  try {
    const b=req.body;
    const t=(db.data.trades||[]).find(t=>(b.deal_id&&t.deal_id===b.deal_id)||(b.ticker&&t.ticker===b.ticker&&['OPEN','PARTIAL'].includes(t.status)));
    if(t&&b.new_sl) t.sl=b.new_sl;
    db.data.updates.unshift({ id:nid('updates'), ts:b.ts||now(), deal_id:b.deal_id||null, ticker:b.ticker||null, scanner:b.scanner||null, action:'UPDATE_SL', old_sl:b.old_sl||null, new_sl:b.new_sl||null });
    await save(); res.json({status:'ok',found:!!t});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/rejection', async (req,res)=>{
  try {
    const b=req.body;
    db.data.rejections.unshift({ id:nid('rejections'), ts:b.ts||now(), scanner:b.scanner||'unknown', ticker:b.ticker||'UNKNOWN', reason:b.reason||'UNKNOWN', detail:b.detail||b.message||'' });
    if(db.data.rejections.length>500) db.data.rejections=db.data.rejections.slice(0,500);
    await save(); res.json({status:'ok'});
  } catch(e){ res.status(500).json({status:'error',message:e.message}); }
});

app.post('/api/ping', async (req,res)=>{
  try {
    const b=req.body;
    db.data.pings.unshift({ id:nid('pings'), ts:b.ts||now(), scanner:b.scanner||'unknown', status:b.status||'running', signals_today:b.signals_today||0, open_trades:b.open_trades||0, portfolio_heat:b.portfolio_heat||0, regime:b.regime||'', message:b.message||'' });
    if(db.data.pings.length>400) db.data.pings=db.data.pings.slice(0,400);
    await save(); res.json({status:'ok'});
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
    res.json({ investors, fund:db.data.fund, fund_stats:fundStats, summary:{ total_aum:parseFloat(totalAUM.toFixed(2)), total_invested:parseFloat(totalIn.toFixed(2)), total_pnl:parseFloat(totalPnl.toFixed(2)), investor_count:investors.filter(i=>i.active).length, pending_withdrawals:pending.length, open_positions:fundStats.open_count }, pending_withdrawals:pending, latest_pings:Object.values(latestPings), today:{ trades:todayTrades.length, signals:todaySignals.length, rejections:todayRejs.length } });
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
    const SCANNERS = ['fmp','forex','comm','pa'];
    const sLabel = {fmp:'FMP Stocks',forex:'Forex',comm:'Commodity',pa:'Price Action'};

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
app.post('/api/eod', adminOnly, async (req,res)=>{
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
app.post('/api/positions/live', adminOnly, async (req,res)=>{
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


// ── Serve frontend ───────────────────────────────────────────
app.get('/investor', (req,res) => res.sendFile(path.join(__dirname,'../public/investor.html')));

// ── Serve frontend (catch-all MUST be last) ───
app.get('*', (req,res) => res.sendFile(path.join(__dirname,'../public/index.html')));

app.listen(PORT, () => {
  console.log(`\n🏦 Fund Management System → http://localhost:${PORT}`);
  console.log(`   Admin:    http://localhost:${PORT}`);
  console.log(`   Investor: http://localhost:${PORT}/investor\n`);
});