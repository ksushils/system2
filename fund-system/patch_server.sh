#!/bin/bash
# This patches the VPS server with missing endpoints
# Run on VPS: bash /tmp/patch_server.sh

SERVER="/root/fund-system/server/index.js"

# Remove old app.listen and catch-all (last 10 lines) and append new code + re-add them
head -n -10 "$SERVER" > /tmp/server_head.js

cat >> /tmp/server_head.js << 'ENDPOINTS'
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
app.get('*', (req,res) => res.sendFile(path.join(__dirname,'../public/index.html')));

ENDPOINTS

# Add app.listen back
echo "" >> /tmp/server_head.js
echo "const PORT = process.env.PORT || 3210;" >> /tmp/server_head.js
echo "app.listen(PORT, () => {" >> /tmp/server_head.js
echo "  console.log();" >> /tmp/server_head.js  
echo "  console.log('🏦 Fund Management System → http://localhost:' + PORT);" >> /tmp/server_head.js
echo "  console.log('   Admin:    http://localhost:' + PORT);" >> /tmp/server_head.js
echo "  console.log('   Investor: http://localhost:' + PORT + '/investor');" >> /tmp/server_head.js
echo "  console.log();" >> /tmp/server_head.js
echo "});" >> /tmp/server_head.js

cp "$SERVER" "$SERVER.backup"
cp /tmp/server_head.js "$SERVER"
node --check "$SERVER" && echo "✓ Syntax OK" || echo "✗ Syntax error"
pm2 restart fund-system
echo "Done"
