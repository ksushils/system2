// ════════════════════════════════════════════════════════════
// PERFORMANCE ANALYTICS ROUTES  v1.0
// Paste this entire block into server/index.js
// BEFORE your existing app.listen() line
// ════════════════════════════════════════════════════════════
//
// Reads from your existing lowdb data (trades, signals, rejections)
// and computes all the metrics needed for the analytics dashboard.
// NO schema changes needed — works with existing data structure.
// ════════════════════════════════════════════════════════════

// ── HELPER: safe number ──────────────────────────────────────
function n(v) { return isFinite(v) && v !== null ? +v : 0; }

// ── HELPER: round to 2dp ────────────────────────────────────
function r2(v) { return Math.round(n(v) * 100) / 100; }

// ── HELPER: date string YYYY-MM-DD from timestamp ───────────
function dateStr(ts) {
  return new Date(ts).toISOString().slice(0, 10);
}

// ── HELPER: compute trade metrics from a list of closed trades
function computeMetrics(trades) {
  const closed = trades.filter(t => t.status === 'CLOSED' && t.pnl !== undefined);
  if (!closed.length) return null;

  const wins   = closed.filter(t => n(t.pnl) > 0);
  const losses = closed.filter(t => n(t.pnl) <= 0);
  const totalPnl   = closed.reduce((s, t) => s + n(t.pnl), 0);
  const grossWin   = wins.reduce((s, t) => s + n(t.pnl), 0);
  const grossLoss  = Math.abs(losses.reduce((s, t) => s + n(t.pnl), 0));
  const winRate    = closed.length ? (wins.length / closed.length) * 100 : 0;
  const avgWin     = wins.length ? grossWin / wins.length : 0;
  const avgLoss    = losses.length ? grossLoss / losses.length : 0;
  const rr         = avgLoss ? avgWin / avgLoss : 0;
  const profitFactor = grossLoss ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0;

  // Max drawdown
  let peak = 0, equity = 0, maxDD = 0;
  for (const t of closed) {
    equity += n(t.pnl);
    if (equity > peak) peak = equity;
    const dd = peak - equity;
    if (dd > maxDD) maxDD = dd;
  }

  // Avg hold time (minutes)
  const withTimes = closed.filter(t => t.openTime && t.closeTime);
  const avgHoldMin = withTimes.length
    ? withTimes.reduce((s, t) => s + (new Date(t.closeTime) - new Date(t.openTime)) / 60000, 0) / withTimes.length
    : 0;

  // Consecutive wins/losses
  let maxConsecWins = 0, maxConsecLoss = 0, cw = 0, cl = 0;
  for (const t of closed) {
    if (n(t.pnl) > 0) { cw++; cl = 0; } else { cl++; cw = 0; }
    if (cw > maxConsecWins) maxConsecWins = cw;
    if (cl > maxConsecLoss) maxConsecLoss = cl;
  }

  return {
    totalTrades:    closed.length,
    wins:           wins.length,
    losses:         losses.length,
    winRate:        r2(winRate),
    totalPnl:       r2(totalPnl),
    grossWin:       r2(grossWin),
    grossLoss:      r2(grossLoss),
    avgWin:         r2(avgWin),
    avgLoss:        r2(avgLoss),
    rrRatio:        r2(rr),
    profitFactor:   r2(profitFactor > 100 ? 99.99 : profitFactor),
    maxDrawdown:    r2(maxDD),
    avgHoldMin:     r2(avgHoldMin),
    maxConsecWins,
    maxConsecLoss,
  };
}

// ════════════════════════════════════════════════════════════
// GET /api/analytics/overview
// Full-time trading scorecard + per-strategy breakdown
// ════════════════════════════════════════════════════════════
app.get('/api/analytics/overview', (req, res) => {
  try {
    const db   = getDb();
    const trades    = db.data.trades    || [];
    const signals   = db.data.signals   || [];
    const rejections = db.data.rejections || [];

    const overall = computeMetrics(trades);

    // Per-strategy breakdown
    const strategies = {};
    for (const t of trades) {
      const s = t.strategy || t.scanner || 'UNKNOWN';
      if (!strategies[s]) strategies[s] = [];
      strategies[s].push(t);
    }
    const byStrategy = Object.entries(strategies).map(([name, ts]) => ({
      name,
      ...computeMetrics(ts),
      tradeCount: ts.length,
    })).sort((a, b) => (b.totalPnl || 0) - (a.totalPnl || 0));

    // Daily P&L (last 60 days)
    const pnlByDay = {};
    for (const t of trades.filter(t => t.status === 'CLOSED' && t.pnl !== undefined)) {
      const d = dateStr(t.closeTime || t.openTime || Date.now());
      pnlByDay[d] = r2((pnlByDay[d] || 0) + n(t.pnl));
    }
    const dailyPnl = Object.entries(pnlByDay)
      .sort(([a], [b]) => a.localeCompare(b))
      .slice(-60)
      .map(([date, pnl]) => ({ date, pnl }));

    // Equity curve
    let equity = 0;
    const equityCurve = dailyPnl.map(({ date, pnl }) => {
      equity += pnl;
      return { date, equity: r2(equity) };
    });

    // Weekly P&L (last 12 weeks)
    const pnlByWeek = {};
    for (const { date, pnl } of dailyPnl) {
      const d = new Date(date);
      const weekStart = new Date(d);
      weekStart.setDate(d.getDate() - d.getDay()); // Sunday
      const wk = weekStart.toISOString().slice(0, 10);
      pnlByWeek[wk] = r2((pnlByWeek[wk] || 0) + pnl);
    }
    const weeklyPnl = Object.entries(pnlByWeek)
      .sort(([a], [b]) => a.localeCompare(b))
      .slice(-12)
      .map(([week, pnl]) => ({ week, pnl }));

    // Monthly P&L
    const pnlByMonth = {};
    for (const { date, pnl } of dailyPnl) {
      const mo = date.slice(0, 7);
      pnlByMonth[mo] = r2((pnlByMonth[mo] || 0) + pnl);
    }
    const monthlyPnl = Object.entries(pnlByMonth)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([month, pnl]) => ({ month, pnl }));

    // Win distribution by hour
    const byHour = Array.from({ length: 24 }, (_, i) => ({ hour: i, wins: 0, losses: 0, pnl: 0 }));
    for (const t of trades.filter(t => t.status === 'CLOSED' && t.openTime)) {
      const h = new Date(t.openTime).getUTCHours();
      if (n(t.pnl) > 0) byHour[h].wins++;
      else byHour[h].losses++;
      byHour[h].pnl = r2(byHour[h].pnl + n(t.pnl));
    }

    // Best / worst trades
    const closed = trades.filter(t => t.status === 'CLOSED' && t.pnl !== undefined);
    const bestTrades  = [...closed].sort((a, b) => n(b.pnl) - n(a.pnl)).slice(0, 5);
    const worstTrades = [...closed].sort((a, b) => n(a.pnl) - n(b.pnl)).slice(0, 5);

    // Full-time readiness score (0-100)
    let readiness = 0;
    const msgs = [];
    if (overall) {
      if (overall.totalTrades >= 50)  { readiness += 20; } else { msgs.push(`Need ${50 - overall.totalTrades} more trades for statistical confidence`); }
      if (overall.winRate >= 50)      { readiness += 20; } else { msgs.push(`Win rate ${overall.winRate}% — target ≥50%`); }
      if (overall.rrRatio >= 1.5)     { readiness += 20; } else { msgs.push(`R:R ratio ${overall.rrRatio} — target ≥1.5`); }
      if (overall.profitFactor >= 1.5){ readiness += 20; } else { msgs.push(`Profit factor ${overall.profitFactor} — target ≥1.5`); }
      if (overall.maxDrawdown < overall.totalPnl * 0.2) { readiness += 20; } else { msgs.push(`Max drawdown ${overall.maxDrawdown} is too high vs total profit`); }
    } else {
      msgs.push('No closed trades yet — start trading to generate data');
    }

    // Signal acceptance rate
    const totalSignals = signals.length;
    const totalRejected = rejections.length;
    const signalAcceptRate = totalSignals
      ? r2(((totalSignals - totalRejected) / totalSignals) * 100)
      : 0;

    // Today's P&L
    const todayStr = dateStr(Date.now());
    const todayPnl = r2(
      trades.filter(t => t.status === 'CLOSED' && dateStr(t.closeTime || t.openTime || 0) === todayStr)
            .reduce((s, t) => s + n(t.pnl), 0)
    );

    // This week P&L
    const now = new Date();
    const weekAgo = new Date(now.getTime() - 7 * 86400000);
    const weekPnl = r2(
      trades.filter(t => t.status === 'CLOSED' && new Date(t.closeTime || t.openTime || 0) >= weekAgo)
            .reduce((s, t) => s + n(t.pnl), 0)
    );

    res.json({
      overall,
      byStrategy,
      dailyPnl,
      weeklyPnl,
      monthlyPnl,
      equityCurve,
      byHour,
      bestTrades,
      worstTrades,
      readiness,
      readinessMessages: msgs,
      signalAcceptRate,
      totalSignals,
      totalRejected,
      todayPnl,
      weekPnl,
    });
  } catch (err) {
    console.error('Analytics error:', err);
    res.status(500).json({ error: err.message });
  }
});

// ════════════════════════════════════════════════════════════
// GET /api/analytics/drilldown?strategy=EARNINGS_CATALYST
// Deep dive on a single strategy
// ════════════════════════════════════════════════════════════
app.get('/api/analytics/drilldown', (req, res) => {
  try {
    const db = getDb();
    const { strategy } = req.query;
    const trades = (db.data.trades || []).filter(t =>
      !strategy || (t.strategy || t.scanner || 'UNKNOWN') === strategy
    );
    const metrics = computeMetrics(trades);
    const recent = [...trades]
      .filter(t => t.status === 'CLOSED')
      .sort((a, b) => new Date(b.closeTime || 0) - new Date(a.closeTime || 0))
      .slice(0, 20);
    res.json({ strategy, metrics, recent });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ════════════════════════════════════════════════════════════
// GET /api/analytics/daily-limit-check
// Returns whether today's loss limit has been hit
// ════════════════════════════════════════════════════════════
app.get('/api/analytics/daily-limit-check', (req, res) => {
  try {
    const db = getDb();
    const dailyLossLimit = n(req.query.limit || 200); // default $200 / change to your preference
    const todayStr = dateStr(Date.now());
    const todayPnl = (db.data.trades || [])
      .filter(t => t.status === 'CLOSED' && dateStr(t.closeTime || t.openTime || 0) === todayStr)
      .reduce((s, t) => s + n(t.pnl), 0);
    res.json({
      todayPnl: r2(todayPnl),
      limitHit: todayPnl <= -dailyLossLimit,
      dailyLossLimit,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ════════════════════════════════════════════════════════════
// END OF ANALYTICS ROUTES
// ════════════════════════════════════════════════════════════
