// ════════════════════════════════════════════════════════════════════
// SCORING LOOP ENDPOINTS  —  paste into fund-system/server/index.js
// just BEFORE the `app.get('*', ...)` catch-all and `app.listen(...)`.
//
// Purpose: log every overnight trade IDEA, then score it against what the
// stock ACTUALLY did at +1d, +3d, +10d. This is the feedback loop that
// tells you whether Chronos and the Council actually help.
//
// Works with lowdb (db.data.*) — same store the rest of the app uses.
// If db.data.ideas doesn't exist it is created on first write.
// ════════════════════════════════════════════════════════════════════

const fs = require('fs');
const path = require('path');
const childProcess = require('child_process');

module.exports = function attachScoring(app, db, deps) {
  const { adminOnly, now, fmpKey, httpGet } = deps;
  const system2Root = process.env.SYSTEM2_CORE_DIR || '/root/system2-core';
  const configPath = path.join(system2Root, 'system2-config.json');
  const envPath = path.join(system2Root, '.env');
  const logDir = path.join(system2Root, 'logs');
  const defaultSystem2Config = {
    mode: 'paper',
    notes: 'Bare core baseline only. C1 is ride-along logging-only. C3/C4/C2/C5 are not wired.',
    universe: { target_size: 800, min_market_cap: 2000000000, min_avg_volume: 0, exclude_price_below: 5 },
    stage1: { min_price: 5, min_avg_volume: 1000000, min_dollar_volume: 20000000, earnings_blackout_days: 5, blocked_tickers: ['STRC'] },
    stage2: { top_n: 40, max_workers: 4 },
    stage7: { account_size: 25000, risk_pct: 0.01, max_trades_per_day: 3, max_portfolio_heat: 0.06, max_names_per_cluster: 2 },
    layers: { options_flow: 'ride_along_logging_only', chronos: 'ride_along_logging_only', news_safety: 'LIVE', council: 'off', options_discovery: 'off', social_sentiment: 'off' },
  };

  function readJson(file, fallback = null) {
    try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
    catch { return fallback; }
  }

  function readEnvValue(key) {
    try {
      const line = fs.readFileSync(envPath, 'utf8').split(/\r?\n/).find(l => l.trim().startsWith(`${key}=`));
      return line ? line.split('=').slice(1).join('=').trim().replace(/^['"]|['"]$/g, '') : null;
    } catch {
      return null;
    }
  }

  const effectiveFmpKey = fmpKey || process.env.FMP_API_KEY || process.env.FMP_KEY || readEnvValue('FMP_API_KEY') || readEnvValue('FMP_KEY');

  function countArtifact(file) {
    const data = readJson(path.join(system2Root, file), null);
    if (Array.isArray(data)) return data.length;
    if (data && typeof data === 'object') return data;
    return null;
  }

  function deepMerge(base, patch) {
    const out = { ...base };
    for (const [key, value] of Object.entries(patch || {})) {
      if (value && typeof value === 'object' && !Array.isArray(value) && base[key] && typeof base[key] === 'object' && !Array.isArray(base[key])) {
        out[key] = deepMerge(base[key], value);
      } else {
        out[key] = value;
      }
    }
    return out;
  }

  function readSystem2Config() {
    return deepMerge(defaultSystem2Config, readJson(configPath, {}));
  }

  function latestRunSummary() {
    try {
      const files = fs.readdirSync(logDir)
        .filter(f => /^phase_b_core_.*\.json$/.test(f))
        .sort();
      if (!files.length) return null;
      const name = files[files.length - 1];
      return { file: name, ...readJson(path.join(logDir, name), {}) };
    } catch {
      return null;
    }
  }

  function lastLines(file, maxLines = 120) {
    try {
      return fs.readFileSync(file, 'utf8').split(/\r?\n/).slice(-maxLines).join('\n');
    } catch {
      return '';
    }
  }

  function ensure() {
    if (!db.data.ideas) db.data.ideas = [];
    if (!db.data.system2_run_metadata) db.data.system2_run_metadata = [];
    if (!db.data.system2_rejections) db.data.system2_rejections = [];
    if (!db.data.system2_monitor_snapshots) db.data.system2_monitor_snapshots = [];
    if (!db.data.system2_stage_details) db.data.system2_stage_details = [];
  }

  function isLocalRequest(req) {
    const ip = req.ip || req.connection?.remoteAddress || '';
    return ip === '127.0.0.1' || ip === '::1' || ip.includes('127.0.0.1') || ip.includes('::ffff:127.0.0.1');
  }

  function localOnly(req, res, next) {
    if (!isLocalRequest(req)) return res.status(403).json({ error: 'Local only' });
    next();
  }

  function ideaAgeDays(row) {
    return Math.floor((new Date() - new Date(row.date + 'T00:00:00Z')) / 86400000);
  }

  function deriveIdeaStatus(row) {
    if (row.paper_exit_reason === 'TARGET' || row.hit === 'TARGET') {
      return { status: 'CLOSED', outcome: 'WIN' };
    }
    if (row.paper_exit_reason === 'STOP' || row.hit === 'STOP') {
      return { status: 'CLOSED', outcome: 'LOSS' };
    }
    if (row.scored_stage >= 10 || row.hit === 'TIME') {
      if (row.r_10d != null && row.r_10d > 0) return { status: 'CLOSED', outcome: 'WIN' };
      if (row.r_10d != null && row.r_10d < 0) return { status: 'CLOSED', outcome: 'LOSS' };
      return { status: 'CLOSED', outcome: 'TIMEOUT' };
    }
    if (ideaAgeDays(row) >= 10 && row.scored_stage >= 10) return { status: 'CLOSED', outcome: 'TIMEOUT' };
    return { status: 'OPEN', outcome: null };
  }

  function withDerivedStatus(row) {
    const s = deriveIdeaStatus(row);
    return { ...row, paper_status: row.paper_status || s.status, paper_outcome: row.paper_outcome || s.outcome };
  }

  function rValue(entry, riskPerShare, price) {
    return riskPerShare && riskPerShare > 0 && price != null
      ? Number(((Number(price) - Number(entry)) / Number(riskPerShare)).toFixed(3))
      : null;
  }

  function simulatedLongExit(row, window, fallbackClose, timeoutHit) {
    const entry = Number(row.entry);
    const stop = row.stop != null ? Number(row.stop) : null;
    const target = row.target != null ? Number(row.target) : null;
    for (const bar of window) {
      const open = Number(bar.open);
      const high = Number(bar.high);
      const low = Number(bar.low);
      if (Number.isFinite(stop) && Number.isFinite(low) && low <= stop) {
        const exitPrice = Number.isFinite(open) && open < stop ? open : stop;
        return { hit: 'STOP', exitPrice, exitDate: bar.date };
      }
      if (Number.isFinite(target) && Number.isFinite(high) && high >= target) {
        const exitPrice = Number.isFinite(open) && open > target ? open : target;
        return { hit: 'TARGET', exitPrice, exitDate: bar.date };
      }
    }
    return { hit: timeoutHit, exitPrice: fallbackClose, exitDate: window[window.length - 1]?.date || null };
  }

  async function fetchDailyHistory(ticker) {
    const url = `https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=${ticker}&apikey=${effectiveFmpKey}`;
    const data = await httpGet(url);
    return (data && data.historical) ? data.historical : (Array.isArray(data) ? data : []);
  }

  function applyMarks(row, hist, marks, today) {
    let updated = 0;
    const ideaDate = new Date(row.date + 'T00:00:00Z');
    const ageDays = Math.floor((today - ideaDate) / 86400000);
    const sorted = [...hist].sort((a,b)=> new Date(a.date)-new Date(b.date));
    const afterEntry = sorted.filter(h => new Date(h.date) > ideaDate);
    for (const m of marks) {
      if ((row.scored_stage || 0) >= m.stage) continue;
      if (ageDays < m.days) continue;
      if (afterEntry.length < m.days) continue;
      const bar = afterEntry[m.days - 1];
      const px = Number(bar.close);
      row[m.field] = px;
      const window = afterEntry.slice(0, m.days);
      const highs = window.map(h => Number(h.high));
      const lows = window.map(h => Number(h.low));
      if (highs.length) row.max_gain_pct = Number((((Math.max(...highs) - row.entry)/row.entry)*100).toFixed(2));
      if (lows.length) row.max_dd_pct = Number((((Math.min(...lows) - row.entry)/row.entry)*100).toFixed(2));
      const exit = simulatedLongExit(row, window, px, m.days >= 10 ? 'TIME' : 'OPEN');
      row.hit = exit.hit;
      row[m.rfield] = rValue(row.entry, row.risk_per_share, exit.exitPrice);
      row.paper_exit_reason = exit.hit === 'STOP' || exit.hit === 'TARGET' ? exit.hit : row.paper_exit_reason;
      row.paper_exit_at = exit.hit === 'STOP' || exit.hit === 'TARGET' ? exit.exitDate : row.paper_exit_at;
      row.paper_exit_price = exit.hit === 'STOP' || exit.hit === 'TARGET' ? exit.exitPrice : row.paper_exit_price;
      row.paper_exit_r = exit.hit === 'STOP' || exit.hit === 'TARGET' ? row[m.rfield] : row.paper_exit_r;
      row.scored_stage = m.stage;
      row.scored_at = now();
      const status = deriveIdeaStatus(row);
      row.paper_status = status.status;
      row.paper_outcome = status.outcome;
      updated++;
    }
    return updated;
  }

  async function quoteMap(symbols) {
    const out = {};
    const unique = [...new Set(symbols.filter(Boolean).map(s => String(s).toUpperCase()))];
    for (let i = 0; i < unique.length; i += 50) {
      const chunk = unique.slice(i, i + 50);
      try {
        const data = await httpGet(`https://financialmodelingprep.com/stable/batch-quote?symbols=${chunk.join(',')}&apikey=${effectiveFmpKey}`);
        const arr = Array.isArray(data) ? data : (data ? [data] : []);
        for (const q of arr) {
          const sym = String(q.symbol || '').toUpperCase();
          const price = Number(q.price || q.close || q.previousClose);
          if (sym && Number.isFinite(price)) out[sym] = price;
        }
      } catch {
        for (const sym of chunk) {
          try {
            const one = await httpGet(`https://financialmodelingprep.com/stable/quote?symbol=${sym}&apikey=${effectiveFmpKey}`);
            const q = Array.isArray(one) ? one[0] : one;
            const price = Number(q?.price || q?.close || q?.previousClose);
            if (Number.isFinite(price)) out[sym] = price;
          } catch {}
        }
      }
    }
    return out;
  }

  function quotePrice(q) {
    const fields = [
      'preMarketPrice', 'premarketPrice', 'preMarket', 'pre_market_price',
      'price', 'close', 'previousClose',
    ];
    for (const field of fields) {
      const v = Number(q?.[field]);
      if (Number.isFinite(v) && v > 0) return { price: v, source: field };
    }
    return { price: null, source: null };
  }

  async function quoteRows(symbols) {
    const out = {};
    const unique = [...new Set(symbols.filter(Boolean).map(s => String(s).toUpperCase()))];
    for (let i = 0; i < unique.length; i += 50) {
      const chunk = unique.slice(i, i + 50);
      try {
        const data = await httpGet(`https://financialmodelingprep.com/stable/batch-quote?symbols=${chunk.join(',')}&apikey=${effectiveFmpKey}`);
        const arr = Array.isArray(data) ? data : (data ? [data] : []);
        for (const q of arr) {
          const sym = String(q.symbol || '').toUpperCase();
          if (sym) out[sym] = q;
        }
      } catch {
        for (const sym of chunk) {
          try {
            const one = await httpGet(`https://financialmodelingprep.com/stable/quote?symbol=${sym}&apikey=${effectiveFmpKey}`);
            const q = Array.isArray(one) ? one[0] : one;
            if (q) out[sym] = q;
          } catch {}
        }
      }
    }
    return out;
  }

  async function sendTelegramAlert(text) {
    const token = process.env.TELEGRAM_BOT_TOKEN || process.env.TG_BOT_TOKEN || readEnvValue('TELEGRAM_BOT_TOKEN') || readEnvValue('TG_BOT_TOKEN');
    const chatId = process.env.TELEGRAM_CHAT_ID || process.env.TG_CHAT_ID || readEnvValue('TELEGRAM_CHAT_ID') || readEnvValue('TG_CHAT_ID');
    if (!token || !chatId) return { sent: false, reason: 'missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID' };
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text }),
    });
    if (!r.ok) return { sent: false, reason: `telegram ${r.status}` };
    return { sent: true };
  }

  async function runPreMarketGapCheck(opts = {}) {
    ensure();
    const dryRun = Boolean(opts.dryRun || opts.dry_run);
    const thresholdAtrMultiple = Number(opts.thresholdAtrMultiple || opts.threshold_atr_multiple || 1.5);
    const at = now();
    const openIdeas = db.data.ideas
      .map(withDerivedStatus)
      .filter(i => i.paper !== false && i.paper_status === 'OPEN' && i.entry);
    const quotes = await quoteRows(openIdeas.map(i => i.ticker));
    const checked = [];
    const alerts = [];
    const errors = [];

    for (const idea of db.data.ideas) {
      const derived = withDerivedStatus(idea);
      if (!(derived.paper !== false && derived.paper_status === 'OPEN' && derived.entry)) continue;
      const ticker = String(idea.ticker).toUpperCase();
      const q = quotes[ticker];
      const { price: pmPrice, source: priceSource } = quotePrice(q);
      const atr = Number(idea.atr14 || idea.atr || idea.risk_per_share);
      const entry = Number(idea.entry);
      const target = Number(idea.target);
      const direction = Number.isFinite(target) && target < entry ? 'SHORT' : 'LONG';
      const update = {
        ticker,
        entry,
        direction,
        pre_market_checked_at: at,
        pre_market_price: pmPrice,
        pre_market_price_source: priceSource,
        pre_market_gap_adverse: false,
        pre_market_gap_favourable: false,
        pre_market_gap_pct: null,
        pre_market_gap_atr_multiple: null,
        pre_market_gap_threshold_atr: thresholdAtrMultiple,
        pre_market_gap_error: null,
      };

      if (!Number.isFinite(pmPrice) || pmPrice <= 0) {
        update.pre_market_gap_error = 'no pre-market/quote price from FMP';
        errors.push(`${ticker}: ${update.pre_market_gap_error}`);
      } else if (!Number.isFinite(atr) || atr <= 0) {
        update.pre_market_gap_error = 'missing ATR; used by 1.5x ATR gap rule';
        errors.push(`${ticker}: ${update.pre_market_gap_error}`);
      } else {
        const move = pmPrice - entry;
        const signedSetupMove = direction === 'LONG' ? move : -move;
        update.pre_market_gap_pct = Number(((move / entry) * 100).toFixed(2));
        update.pre_market_gap_atr_multiple = Number((Math.abs(move) / atr).toFixed(2));
        update.pre_market_gap_adverse = signedSetupMove <= -(thresholdAtrMultiple * atr);
        update.pre_market_gap_favourable = signedSetupMove >= (thresholdAtrMultiple * atr);
        if (update.pre_market_gap_adverse && !dryRun) {
          const gapDirection = move < 0 ? 'down' : 'up';
          const msg = `⚠️ PRE-MARKET GAP: ${ticker} gapped ${gapDirection} ${Math.abs(update.pre_market_gap_pct)}% against setup. Review before market open.`;
          try {
            const sent = await sendTelegramAlert(msg);
            alerts.push({ ticker, message: msg, ...sent });
          } catch (e) {
            alerts.push({ ticker, message: msg, sent: false, reason: e.message });
          }
        }
      }

      checked.push(update);
      if (!dryRun) Object.assign(idea, update);
    }

    if (!dryRun) await db.write();
    return {
      ok: true,
      dryRun,
      checked: openIdeas.length,
      updated: dryRun ? 0 : checked.length,
      fmpCallEstimate: Math.max(1, Math.ceil(openIdeas.length / 50)),
      adverse: checked.filter(x => x.pre_market_gap_adverse).length,
      favourable: checked.filter(x => x.pre_market_gap_favourable).length,
      neutral: checked.filter(x => !x.pre_market_gap_adverse && !x.pre_market_gap_favourable && !x.pre_market_gap_error).length,
      errors: errors.slice(0, 50),
      alerts,
      results: checked,
    };
  }

  async function runPaperMonitor() {
    ensure();
    const at = now();
    const openIdeas = db.data.ideas
      .map(withDerivedStatus)
      .filter(i => i.paper !== false && i.paper_status === 'OPEN' && i.entry && i.risk_per_share);
    const prices = await quoteMap(openIdeas.map(i => i.ticker));
    const missingPrices = [];
    const snapshots = [];
    for (const idea of db.data.ideas) {
      const derived = withDerivedStatus(idea);
      if (!(derived.paper !== false && derived.paper_status === 'OPEN' && derived.entry && derived.risk_per_share)) continue;
      const px = prices[String(idea.ticker).toUpperCase()];
      if (!px) {
        missingPrices.push(idea.ticker);
        continue;
      }
      const unrealizedR = rValue(idea.entry, idea.risk_per_share, px);
      const gainPct = Number((((px - idea.entry) / idea.entry) * 100).toFixed(2));
      idea.current_price = px;
      idea.unrealized_r = unrealizedR;
      idea.current_gain_pct = gainPct;
      idea.distance_to_target_pct = idea.target ? Number((((idea.target - px) / px) * 100).toFixed(2)) : null;
      idea.distance_to_stop_pct = idea.stop ? Number((((px - idea.stop) / px) * 100).toFixed(2)) : null;
      idea.monitor_peak_gain_pct = idea.monitor_peak_gain_pct == null ? gainPct : Math.max(idea.monitor_peak_gain_pct, gainPct);
      idea.monitor_worst_dd_pct = idea.monitor_worst_dd_pct == null ? gainPct : Math.min(idea.monitor_worst_dd_pct, gainPct);
      idea.monitor_updated_at = at;
      let wouldExit = null;
      if (idea.target && px >= idea.target) wouldExit = 'TARGET';
      if (idea.stop && px <= idea.stop) wouldExit = 'STOP';
      if (wouldExit && !idea.paper_exit_reason) {
        idea.paper_exit_reason = wouldExit;
        idea.paper_exit_at = at;
        idea.paper_exit_price = px;
        idea.paper_exit_r = unrealizedR;
        idea.paper_status = 'CLOSED';
        idea.paper_outcome = wouldExit === 'TARGET' ? 'WIN' : 'LOSS';
      }
      snapshots.push({
        id: `${at}_${idea.id}`,
        idea_id: idea.id,
        date: idea.date,
        ticker: idea.ticker,
        checked_at: at,
        current_price: px,
        unrealized_r: unrealizedR,
        current_gain_pct: gainPct,
        distance_to_target_pct: idea.distance_to_target_pct,
        distance_to_stop_pct: idea.distance_to_stop_pct,
        peak_gain_pct: idea.monitor_peak_gain_pct,
        worst_dd_pct: idea.monitor_worst_dd_pct,
        would_exit: wouldExit,
        paper_only: true,
      });
    }
    db.data.system2_monitor_snapshots.push(...snapshots);
    if (db.data.system2_monitor_snapshots.length > 5000) {
      db.data.system2_monitor_snapshots = db.data.system2_monitor_snapshots.slice(-5000);
    }
    await db.write();
    return { ok: true, checked: openIdeas.length, updated: snapshots.length, missingPrices: missingPrices.slice(0, 50), snapshots };
  }

  function dateRangeFromQuery(req) {
    const preset = String(req.query.preset || '30d');
    const today = new Date();
    const iso = d => d.toISOString().slice(0, 10);
    if (req.query.from || req.query.to) {
      return {
        from: req.query.from || '0000-01-01',
        to: req.query.to || '9999-12-31',
        preset: 'custom',
      };
    }
    if (preset === 'all') return { from: '0000-01-01', to: '9999-12-31', preset };
    const days = preset === '7d' ? 7 : preset === '90d' ? 90 : 30;
    const start = new Date(today.getTime() - days * 86400000);
    return { from: iso(start), to: iso(today), preset };
  }

  function scoredValue(row, field) {
    if (row[field] == null || row[field] === '') return null;
    const v = Number(row[field]);
    return Number.isFinite(v) ? v : null;
  }

  function cohortStats(rows, rfield = 'r_3d', minSample = 30) {
    const scored = rows.filter(r => scoredValue(r, rfield) != null);
    const values = scored.map(r => scoredValue(r, rfield));
    const wins = values.filter(v => v > 0).length;
    const avg = values.length ? values.reduce((a,b)=>a+b,0) / values.length : null;
    return {
      count: scored.length,
      ready: scored.length >= minSample,
      insufficient: scored.length < minSample ? `insufficient sample (${scored.length}/${minSample})` : null,
      win_rate: scored.length >= minSample ? Math.round((wins / scored.length) * 100) : null,
      avg_r: scored.length >= minSample && avg != null ? Number(avg.toFixed(3)) : null,
      raw_win_rate: scored.length ? Math.round((wins / scored.length) * 100) : null,
      raw_avg_r: avg != null ? Number(avg.toFixed(3)) : null,
    };
  }

  function groupStats(rows, keyFn, rfield = 'r_3d') {
    const groups = {};
    for (const row of rows) {
      const key = keyFn(row) || 'unknown';
      if (!groups[key]) groups[key] = [];
      groups[key].push(row);
    }
    return Object.fromEntries(Object.entries(groups).map(([key, vals]) => [key, cohortStats(vals, rfield)]));
  }

  function verdict(withStats, withoutStats) {
    if (!withStats.ready || !withoutStats.ready) {
      const n = Math.min(withStats.count, withoutStats.count);
      return { verdict: 'ACCUMULATING', badge: `still accumulating - ${n}/30`, ready: false };
    }
    const edge = Number((withStats.raw_avg_r - withoutStats.raw_avg_r).toFixed(3));
    return {
      verdict: edge >= 0.05 ? 'KEEP' : 'DROP',
      ready: true,
      edge_delta_r: edge,
      rule: 'with cohort must beat without by >= 0.05R after >=30 scored ideas',
    };
  }

  function histogram(values) {
    const bins = [
      { label: '<=-2R', min: -Infinity, max: -2, count: 0 },
      { label: '-2 to -1R', min: -2, max: -1, count: 0 },
      { label: '-1 to 0R', min: -1, max: 0, count: 0 },
      { label: '0 to +1R', min: 0, max: 1, count: 0 },
      { label: '+1 to +2R', min: 1, max: 2, count: 0 },
      { label: '>=+2R', min: 2, max: Infinity, count: 0 },
    ];
    for (const v of values.filter(v => v != null && Number.isFinite(v))) {
      const bin = bins.find(b => v >= b.min && v < b.max);
      if (bin) bin.count++;
    }
    return bins.map(({ label, count }) => ({ label, count }));
  }

  function statisticsPayload(req) {
    ensure();
    const range = dateRangeFromQuery(req);
    const rows = db.data.ideas.map(withDerivedStatus).filter(r => r.date >= range.from && r.date <= range.to);
    const scored3 = rows.filter(r => scoredValue(r, 'r_3d') != null);
    const resolved = rows.filter(r => r.paper_status === 'CLOSED' || r.r_3d != null || r.r_10d != null);
    const active = rows.filter(r => r.paper_status === 'OPEN');
    const headlineStats = cohortStats(rows, 'r_3d');
    const scoredValues = scored3.map(r => scoredValue(r, 'r_3d'));
    const best = scored3.slice().sort((a,b)=>b.r_3d-a.r_3d)[0] || null;
    const worst = scored3.slice().sort((a,b)=>a.r_3d-b.r_3d)[0] || null;
    const sources = ['scanner', 'catalyst', 'X', 'options_flag', 'vanta'];
    const bySource = Object.fromEntries(sources.map(src => [
      src,
      cohortStats(rows.filter(r => (r.source || 'scanner') === src), 'r_3d')
    ]));
    const catalystRows = rows.filter(r => (r.source || '') === 'catalyst');
    const catalystBySubType = groupStats(catalystRows, r => r.sub_type || r.catalyst_type || 'unknown', 'r_3d');
    const optionsGroups = {
      CONFIRM: cohortStats(rows.filter(r => r.options_verdict === 'CONFIRM'), 'r_3d'),
      NEUTRAL: cohortStats(rows.filter(r => r.options_verdict === 'NEUTRAL'), 'r_3d'),
      CAUTION: cohortStats(rows.filter(r => r.options_verdict === 'CAUTION'), 'r_3d'),
      NO_DATA: cohortStats(rows.filter(r => r.options_verdict === 'NO_DATA'), 'r_3d'),
    };
    const optionsWith = rows.filter(r => r.options_verdict === 'CONFIRM');
    const optionsWithout = rows.filter(r => r.options_verdict !== 'CONFIRM');
    const tight = rows.filter(r => r.chronos_band_pct != null && Number(r.chronos_band_pct) <= 3);
    const wide = rows.filter(r => r.chronos_band_pct != null && Number(r.chronos_band_pct) > 3);
    const council3 = rows.filter(r => Number(r.council_votes) >= 3);
    const council2 = rows.filter(r => Number(r.council_votes) === 2);
    const councilSingle = rows.filter(r => Number(r.council_votes) === 1);
    const preMarketAdverse = rows.filter(r => r.pre_market_gap_adverse === true);
    const preMarketFavourable = rows.filter(r => r.pre_market_gap_favourable === true);
    const preMarketNeutral = rows.filter(r => r.pre_market_checked_at && !r.pre_market_gap_adverse && !r.pre_market_gap_favourable && !r.pre_market_gap_error);
    const preMarketUnchecked = rows.filter(r => !r.pre_market_checked_at);
    const highConfluence = rows.filter(r => scoredValue(r, 'confluence_score') > 90);
    const mediumConfluence = rows.filter(r => {
      const score = scoredValue(r, 'confluence_score');
      return score != null && score >= 75 && score <= 90;
    });
    const lowConfluence = rows.filter(r => {
      const score = scoredValue(r, 'confluence_score');
      return score != null && score < 75;
    });
    return {
      ok: true,
      range,
      min_sample: 30,
      headline: {
        total_ideas: rows.length,
        resolved_count: resolved.length,
        percent_resolved: rows.length ? Math.round((resolved.length / rows.length) * 100) : 0,
        current_open_count: active.length,
        scored_3d_count: scored3.length,
        win_rate: headlineStats.win_rate,
        avg_r: headlineStats.avg_r,
        expectancy_r: headlineStats.avg_r,
        insufficient: headlineStats.insufficient,
        best_trade: best ? { date: best.date, ticker: best.ticker, r_3d: best.r_3d, hit: best.hit } : null,
        worst_trade: worst ? { date: worst.date, ticker: worst.ticker, r_3d: worst.r_3d, hit: worst.hit } : null,
      },
      by_source: bySource,
      catalyst_sub_type: catalystBySubType,
      by_confluence: {
        high: cohortStats(highConfluence, 'r_3d'),
        medium: cohortStats(mediumConfluence, 'r_3d'),
        low: cohortStats(lowConfluence, 'r_3d'),
      },
      layer_verdicts: {
        options: {
          groups: optionsGroups,
          decision: verdict(cohortStats(optionsWith, 'r_3d'), cohortStats(optionsWithout, 'r_3d')),
        },
        chronos: {
          tight_band: cohortStats(tight, 'r_3d'),
          wide_band: cohortStats(wide, 'r_3d'),
          decision: verdict(cohortStats(tight, 'r_3d'), cohortStats(wide, 'r_3d')),
        },
        council: {
          three_of_three: cohortStats(council3, 'r_3d'),
          two_of_three: cohortStats(council2, 'r_3d'),
          single_model: cohortStats(councilSingle, 'r_3d'),
          decision: verdict(cohortStats(council3, 'r_3d'), cohortStats([...council2, ...councilSingle], 'r_3d')),
        },
        pre_market_gap: {
          adverse: cohortStats(preMarketAdverse, 'r_3d'),
          favourable: cohortStats(preMarketFavourable, 'r_3d'),
          neutral: cohortStats(preMarketNeutral, 'r_3d'),
          unchecked: cohortStats(preMarketUnchecked, 'r_3d'),
          decision: verdict(cohortStats(preMarketFavourable, 'r_3d'), cohortStats([...preMarketAdverse, ...preMarketNeutral], 'r_3d')),
        },
      },
      by_horizon: {
        '1d': cohortStats(rows, 'r_1d'),
        '3d': cohortStats(rows, 'r_3d'),
        '10d': cohortStats(rows, 'r_10d'),
      },
      by_sector: groupStats(rows, r => r.sector || 'unknown', 'r_3d'),
      by_regime: groupStats(rows, r => r.market_regime || r.regime || 'untagged', 'r_3d'),
      distribution: histogram(scoredValues),
      notes: [
        'Cells hide win rate and avg R until >=30 scored ideas.',
        'Regime is reported as untagged until ideas include market_regime/regime.',
        'This endpoint is measurement-only and does not alter funnel selection.',
      ],
    };
  }

  // ── 1. LOG AN IDEA  (called by the nightly n8n funnel, per finalist) ──
  // Body: { date, ticker, mode, entry, stop, target, council_votes,
  //         council_conf, chronos_dir, chronos_conf, chronos_band_pct,
  //         sector, setup, paper:true }
  app.post('/api/idea', async (req, res) => {
    try {
      ensure();
      const b = req.body || {};
      if (!b.ticker || !b.entry) return res.status(400).json({ error: 'ticker and entry required' });
      const ideaDate = b.date || now().slice(0, 10);
      const ideaTicker = String(b.ticker).toUpperCase();
      const ideaSource = b.source || null;
      const ideaPaper = b.paper !== false;
      const existing = db.data.ideas.find(i =>
        i.date === ideaDate &&
        i.ticker === ideaTicker &&
        (i.source || null) === ideaSource &&
        i.paper === ideaPaper
      );
      if (existing) {
        const refreshFields = [
          'chronos_dir', 'chronos_status', 'chronos2_1d', 'chronos2_3d', 'chronos2_5d',
          'forecastDecision', 'forecastTier', 'options_verdict', 'options_notes',
          'news_safety_status', 'hard_landmine', 'analyst_change',
          'regime', 'market_regime', 'regime_reason',
          'council_tier', 'council_size_mult', 'council_reasons',
          'confluence_bonuses', 'kronos_status', 'kronos_dir',
          'combined_forecast_dir'
        ];
        for (const field of refreshFields) {
          if (b[field] !== undefined) existing[field] = b[field];
        }
        if (b.chronos_conf != null) existing.chronos_conf = Number(b.chronos_conf);
        if (b.chronos_conviction != null) existing.chronos_conviction = Number(b.chronos_conviction);
        if (b.chronos_band_pct != null) existing.chronos_band_pct = Number(b.chronos_band_pct);
        if (b.kronos_band_pct != null) existing.kronos_band_pct = Number(b.kronos_band_pct);
        if (b.kronos_conviction != null) existing.kronos_conviction = Number(b.kronos_conviction);
        if (b.kronos_1d != null) existing.kronos_1d = Number(b.kronos_1d);
        if (b.kronos_3d != null) existing.kronos_3d = Number(b.kronos_3d);
        if (b.kronos_5d != null) existing.kronos_5d = Number(b.kronos_5d);
        if (b.models_agree != null) existing.models_agree = b.models_agree === true;
        if (b.combined_band_pct != null) existing.combined_band_pct = Number(b.combined_band_pct);
        if (b.confluence_bonus_from_forecast != null) existing.confluence_bonus_from_forecast = Number(b.confluence_bonus_from_forecast);
        if (b.setup_score != null) existing.setup_score = Number(b.setup_score);
        if (b.confluence_score != null) existing.confluence_score = Number(b.confluence_score);
        if (b.forecastConviction != null) existing.forecastConviction = Number(b.forecastConviction);
        if (Array.isArray(b.forecastReasons)) existing.forecastReasons = b.forecastReasons;
        if (b.options_signals_count != null) existing.options_signals_count = Number(b.options_signals_count);
        if (b.news_recent_items_checked != null) existing.news_recent_items_checked = Number(b.news_recent_items_checked);
        if (b.iv_rank != null) existing.iv_rank = Number(b.iv_rank);
        if (b.vol_oi_ratio != null) existing.vol_oi_ratio = Number(b.vol_oi_ratio);
        if (b.put_call_vol_ratio != null) existing.put_call_vol_ratio = Number(b.put_call_vol_ratio);
        if (b.call_oi_skew != null) existing.call_oi_skew = Number(b.call_oi_skew);
        if (b.atr14 != null) existing.atr14 = Number(b.atr14);
        if (b.atrPct != null) existing.atrPct = Number(b.atrPct);
        if (b.spy_1d_pct != null) existing.spy_1d_pct = Number(b.spy_1d_pct);
        if (b.qqq_1d_pct != null) existing.qqq_1d_pct = Number(b.qqq_1d_pct);
        if (b.vix_current != null) existing.vix_current = Number(b.vix_current);
        if (b.vix_1d_chg != null) existing.vix_1d_chg = Number(b.vix_1d_chg);
        if (b.council_votes != null) existing.council_votes = Number(b.council_votes);
        if (b.council_conf != null) existing.council_conf = Number(b.council_conf);
        if (b.council_size_mult != null) existing.council_size_mult = Number(b.council_size_mult);
        if (Array.isArray(b.council_upgrade_sigs)) existing.council_upgrade_sigs = b.council_upgrade_sigs;
        if (Array.isArray(b.council_red_flags)) existing.council_red_flags = b.council_red_flags;
        for (const field of ['council_claude', 'council_gpt', 'council_gemini']) {
          if (b[field] !== undefined) existing[field] = b[field];
        }
        for (const field of ['council_claude_conf', 'council_gpt_conf', 'council_gemini_conf']) {
          if (b[field] != null) existing[field] = Number(b[field]);
        }
        if (b.council_force_skip != null) existing.council_force_skip = b.council_force_skip === true;
        existing.enrichment_refreshed_at = now();
        await db.write();
        return res.json({ ok: true, duplicate: true, refreshed: true, id: existing.id });
      }
      const idea = {
        id: `${ideaDate}_${ideaTicker}_${Date.now()}`,
        logged_at: now(),
        date: ideaDate,
        ticker: ideaTicker,
        mode: b.mode || 'SWING',            // SWING | DAY
        paper: ideaPaper,                   // default paper TRUE
        entry: Number(b.entry),
        stop: b.stop != null ? Number(b.stop) : null,
        target: b.target != null ? Number(b.target) : null,
        risk_per_share: (b.entry != null && b.stop != null) ? Number(b.entry) - Number(b.stop) : null,
        source: b.source || null,
        grade: b.grade || null,
        sector: b.sector || null,
        setup: b.setup || null,
        sub_type: b.sub_type || b.catalyst_type || null,
        sub_types: Array.isArray(b.sub_types) ? b.sub_types : null,
        catalyst_summary: b.catalyst_summary || null,
        catalyst_date: b.catalyst_date || null,
        catalyst_datetime: b.catalyst_datetime || null,
        catalyst_score: b.catalyst_score != null ? Number(b.catalyst_score) : null,
        catalyst_sources: Array.isArray(b.catalyst_sources) ? b.catalyst_sources : null,
        market_regime: b.market_regime || b.regime || null,
        regime: b.regime || b.market_regime || null,
        regime_reason: b.regime_reason || null,
        spy_1d_pct: b.spy_1d_pct != null ? Number(b.spy_1d_pct) : null,
        qqq_1d_pct: b.qqq_1d_pct != null ? Number(b.qqq_1d_pct) : null,
        vix_current: b.vix_current != null ? Number(b.vix_current) : null,
        vix_1d_chg: b.vix_1d_chg != null ? Number(b.vix_1d_chg) : null,
        setup_score: b.setup_score != null ? Number(b.setup_score) : (b.convictionScore != null ? Number(b.convictionScore) : null),
        confluence_score: b.confluence_score != null ? Number(b.confluence_score) : null,
        confluence_bonuses: Array.isArray(b.confluence_bonuses) ? b.confluence_bonuses : [],
        atr14: b.atr14 != null ? Number(b.atr14) : (b.atr != null ? Number(b.atr) : null),
        atrPct: b.atrPct != null ? Number(b.atrPct) : null,
        // ── attribution: who said what, so we can score them later ──
        council_votes: b.council_votes != null ? Number(b.council_votes) : null, // 0-3
        council_conf:  b.council_conf  != null ? Number(b.council_conf)  : null, // 0-100
        council_tier: b.council_tier || null,
        council_size_mult: b.council_size_mult != null ? Number(b.council_size_mult) : null,
        council_upgrade_sigs: Array.isArray(b.council_upgrade_sigs) ? b.council_upgrade_sigs : null,
        council_red_flags: Array.isArray(b.council_red_flags) ? b.council_red_flags : null,
        council_claude: b.council_claude || null,
        council_gpt: b.council_gpt || null,
        council_gemini: b.council_gemini || null,
        council_claude_conf: b.council_claude_conf != null ? Number(b.council_claude_conf) : null,
        council_gpt_conf: b.council_gpt_conf != null ? Number(b.council_gpt_conf) : null,
        council_gemini_conf: b.council_gemini_conf != null ? Number(b.council_gemini_conf) : null,
        council_reasons: b.council_reasons || null,
        council_force_skip: b.council_force_skip === true,
        chronos_dir:   b.chronos_dir   || null,        // UP | DOWN | FLAT
        chronos_conf:  b.chronos_conf  != null ? Number(b.chronos_conf) : null,  // 0-100
        chronos_conviction: b.chronos_conviction != null ? Number(b.chronos_conviction) : null,
        chronos_band_pct: b.chronos_band_pct != null ? Number(b.chronos_band_pct) : null, // quantile spread width %
        chronos_status: b.chronos_status || null,
        chronos2_1d: b.chronos2_1d || null,
        chronos2_3d: b.chronos2_3d || null,
        chronos2_5d: b.chronos2_5d || null,
        forecastConviction: b.forecastConviction != null ? Number(b.forecastConviction) : null,
        forecastDecision: b.forecastDecision || null,
        forecastTier: b.forecastTier || null,
        forecastReasons: Array.isArray(b.forecastReasons) ? b.forecastReasons : null,
        kronos_status: b.kronos_status || null,
        kronos_dir: b.kronos_dir || null,
        kronos_band_pct: b.kronos_band_pct != null ? Number(b.kronos_band_pct) : null,
        kronos_conviction: b.kronos_conviction != null ? Number(b.kronos_conviction) : null,
        kronos_1d: b.kronos_1d != null ? Number(b.kronos_1d) : null,
        kronos_3d: b.kronos_3d != null ? Number(b.kronos_3d) : null,
        kronos_5d: b.kronos_5d != null ? Number(b.kronos_5d) : null,
        combined_forecast_dir: b.combined_forecast_dir || null,
        models_agree: b.models_agree === true,
        combined_band_pct: b.combined_band_pct != null ? Number(b.combined_band_pct) : null,
        confluence_bonus_from_forecast: b.confluence_bonus_from_forecast != null
          ? Number(b.confluence_bonus_from_forecast)
          : null,
        options_verdict: b.options_verdict || null, // CONFIRM | NEUTRAL | CAUTION | NO_DATA
        options_signals_count: b.options_signals_count != null
          ? Number(b.options_signals_count)
          : (b.signals_count != null ? Number(b.signals_count) : null),
        iv_rank: b.iv_rank != null
          ? Number(b.iv_rank)
          : (b.iv_rank_proxy != null ? Number(b.iv_rank_proxy) : null),
        vol_oi_ratio: b.vol_oi_ratio != null
          ? Number(b.vol_oi_ratio)
          : (b.call_vol_oi_ratio != null ? Number(b.call_vol_oi_ratio) : null),
        put_call_vol_ratio: b.put_call_vol_ratio != null ? Number(b.put_call_vol_ratio) : null,
        call_oi_skew: b.call_oi_skew != null ? Number(b.call_oi_skew) : null,
        options_notes: b.options_notes || b.notes || null,
        analyst_change: b.analyst_change || null,
        news_safety_status: b.news_safety_status || null,
        news_recent_items_checked: b.news_recent_items_checked != null ? Number(b.news_recent_items_checked) : null,
        hard_landmine: b.hard_landmine || null,
        // ── outcomes, filled in later by the scorer ──
        px_1d: null, px_3d: null, px_10d: null,
        r_1d: null,  r_3d: null,  r_10d: null,
        max_gain_pct: null, max_dd_pct: null,
        hit: null,                          // TARGET | STOP | OPEN | TIME
        paper_status: 'OPEN',
        paper_outcome: null,
        paper_exit_reason: null,
        paper_exit_at: null,
        paper_exit_price: null,
        paper_exit_r: null,
        current_price: null,
        unrealized_r: null,
        current_gain_pct: null,
        distance_to_target_pct: null,
        distance_to_stop_pct: null,
        monitor_peak_gain_pct: null,
        monitor_worst_dd_pct: null,
        monitor_updated_at: null,
        pre_market_checked_at: null,
        pre_market_price: null,
        pre_market_price_source: null,
        pre_market_gap_adverse: false,
        pre_market_gap_favourable: false,
        pre_market_gap_pct: null,
        pre_market_gap_atr_multiple: null,
        pre_market_gap_threshold_atr: 1.5,
        pre_market_gap_error: null,
        chronos_helped: null,               // true/false after scoring
        council_helped: null,
        scored_at: null,
        scored_stage: 0                     // 0=new, 1=after1d, 3=after3d, 10=done
      };
      db.data.ideas.push(idea);
      await db.write();
      res.json({ ok: true, id: idea.id });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  // ── 2. SCORE DUE IDEAS  (called by n8n on a daily schedule) ──
  // Looks up each idea whose +1/+3/+10 day mark has passed and fills outcomes.
  app.post('/api/score/run', async (req, res) => {
    try {
      ensure();
      const today = new Date();
      const out = { checked: 0, updated: 0, checkedNearMisses: 0, updatedNearMisses: 0, errors: [] };
      const marks = [
        { days: 1,  field: 'px_1d',  rfield: 'r_1d',  stage: 1 },
        { days: 3,  field: 'px_3d',  rfield: 'r_3d',  stage: 3 },
        { days: 10, field: 'px_10d', rfield: 'r_10d', stage: 10 },
      ];

      for (const idea of db.data.ideas) {
        if (idea.scored_stage >= 10) continue;
        out.checked++;
        const ideaDate = new Date(idea.date + 'T00:00:00Z');
        const ageDays = Math.floor((today - ideaDate) / 86400000);

        // pull recent daily closes once per ticker per run
        let hist = null;
        for (const m of marks) {
          if (idea.scored_stage >= m.stage) continue;     // already done this mark
          if (ageDays < m.days) continue;                 // not due yet
          if (!hist) {
            try {
              hist = await fetchDailyHistory(idea.ticker);
            } catch (e) { out.errors.push(`${idea.ticker}: ${e.message}`); hist = []; }
          }
          if (!hist.length) continue;
          out.updated += applyMarks(idea, hist, marks, today);
          break;
        }

        // ── attribution scoring once we have the 3-day result ──
        if (idea.scored_stage >= 3 && idea.r_3d != null && idea.chronos_helped == null) {
          const wentRight = idea.r_3d > 0;
          if (idea.chronos_dir) {
            const chronosSaidUp = idea.chronos_dir === 'UP';
            idea.chronos_helped = (chronosSaidUp === wentRight);
          }
          if (idea.council_votes != null) {
            // council "helped" if high agreement matched a win, or low agreement matched a loss
            const highAgree = idea.council_votes >= 2;
            idea.council_helped = (highAgree === wentRight);
          }
        }
      }
      for (const row of db.data.system2_rejections.filter(r => r.near_miss && r.price_scoring_eligible)) {
        if ((row.scored_stage || 0) >= 10) continue;
        out.checkedNearMisses++;
        row.risk_per_share = row.risk_per_share || ((row.entry != null && row.stop != null) ? Number(row.entry) - Number(row.stop) : null);
        try {
          const hist = await fetchDailyHistory(row.ticker);
          out.updatedNearMisses += applyMarks(row, hist, marks, today);
        } catch (e) {
          out.errors.push(`near-miss ${row.ticker}: ${e.message}`);
        }
      }
      await db.write();
      res.json(out);
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  // ── 3. STATS  (dashboard reads this to show if the system actually works) ──
  app.get('/api/score/stats', adminOnly, (req, res) => {
    try {
      ensure();
      const scored = db.data.ideas.filter(i => i.r_3d != null);
      const n = scored.length;
      const avg = (arr) => arr.length ? arr.reduce((a,b)=>a+b,0)/arr.length : 0;

      const r3 = scored.map(i => i.r_3d);
      const wins = r3.filter(r => r > 0).length;

      // does high council agreement actually beat low?
      const hi = scored.filter(i => (i.council_votes||0) >= 3);
      const mid = scored.filter(i => (i.council_votes||0) === 2);
      // does a tight chronos band actually predict better?
      const tight = scored.filter(i => i.chronos_band_pct != null && i.chronos_band_pct <= 3);
      const wide  = scored.filter(i => i.chronos_band_pct != null && i.chronos_band_pct > 3);
      const confluenceStats = (rows) => {
        const values = rows.map(i => i.r_3d);
        return {
          count: rows.length,
          win_rate: rows.length ? Math.round((values.filter(v => v > 0).length / rows.length) * 100) : null,
          avg_r: rows.length ? Number(avg(values).toFixed(3)) : null,
        };
      };

      res.json({
        total_ideas: db.data.ideas.length,
        scored_3d: n,
        win_rate_3d: n ? Math.round((wins/n)*100) : null,
        avg_r_3d: n ? Number(avg(r3).toFixed(3)) : null,
        chronos_accuracy: (() => {
          const c = scored.filter(i => i.chronos_helped != null);
          return c.length ? Math.round((c.filter(i=>i.chronos_helped).length / c.length)*100) : null;
        })(),
        council_accuracy: (() => {
          const c = scored.filter(i => i.council_helped != null);
          return c.length ? Math.round((c.filter(i=>i.council_helped).length / c.length)*100) : null;
        })(),
        tier_compare: {
          council_3of3_avg_r: hi.length ? Number(avg(hi.map(i=>i.r_3d)).toFixed(3)) : null,
          council_2of3_avg_r: mid.length ? Number(avg(mid.map(i=>i.r_3d)).toFixed(3)) : null,
        },
        chronos_band_compare: {
          tight_band_avg_r: tight.length ? Number(avg(tight.map(i=>i.r_3d)).toFixed(3)) : null,
          wide_band_avg_r:  wide.length  ? Number(avg(wide.map(i=>i.r_3d)).toFixed(3))  : null,
        },
        options_compare: (() => {
          const confirmed = scored.filter(i => i.options_verdict === 'CONFIRM' || (i.options_signals_count || 0) >= 2);
          const notConfirmed = scored.filter(i => !(i.options_verdict === 'CONFIRM' || (i.options_signals_count || 0) >= 2));
          return {
            confirm_count: confirmed.length,
            confirm_avg_r: confirmed.length ? Number(avg(confirmed.map(i=>i.r_3d)).toFixed(3)) : null,
            non_confirm_count: notConfirmed.length,
            non_confirm_avg_r: notConfirmed.length ? Number(avg(notConfirmed.map(i=>i.r_3d)).toFixed(3)) : null,
          };
        })(),
        by_confluence: {
          high: confluenceStats(scored.filter(i => i.confluence_score != null && Number(i.confluence_score) > 90)),
          medium: confluenceStats(scored.filter(i => i.confluence_score != null && Number(i.confluence_score) >= 75 && Number(i.confluence_score) <= 90)),
          low: confluenceStats(scored.filter(i => i.confluence_score != null && Number(i.confluence_score) < 75)),
        },
        source_compare: (() => {
          const bySource = {};
          for (const row of scored) {
            const src = row.source || 'unknown';
            if (!bySource[src]) bySource[src] = [];
            bySource[src].push(row.r_3d);
          }
          return Object.fromEntries(Object.entries(bySource).map(([src, vals]) => [
            src,
            { count: vals.length, avg_r_3d: vals.length ? Number(avg(vals).toFixed(3)) : null }
          ]));
        })(),
        note: n < 30
          ? `Only ${n} scored ideas. Need ~30+ before these numbers mean anything.`
          : `Sample size ${n} — numbers are becoming meaningful.`
      });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  // ── 4. RAW IDEAS  (for a dashboard table) ──
  app.get('/api/ideas', adminOnly, (req, res) => {
    ensure();
    const { date, ticker, mode, status, source, from, to, outcome, limit = 200 } = req.query;
    let rows = [...db.data.ideas].map(withDerivedStatus).reverse();
    if (date)   rows = rows.filter(i => i.date === date);
    if (ticker) rows = rows.filter(i => i.ticker === String(ticker).toUpperCase());
    if (mode)   rows = rows.filter(i => i.mode === mode);
    if (status) rows = rows.filter(i => i.paper_status === String(status).toUpperCase());
    if (source) rows = rows.filter(i => (i.source || 'scanner') === source);
    if (outcome) rows = rows.filter(i => (i.paper_outcome || i.hit || 'OPEN') === String(outcome).toUpperCase());
    if (from) rows = rows.filter(i => i.date >= from);
    if (to) rows = rows.filter(i => i.date <= to);
    res.json({ ideas: rows.slice(0, parseInt(limit)), total: rows.length });
  });

  app.get('/api/score/performance', adminOnly, (req, res) => {
    ensure();
    const rows = db.data.ideas.map(withDerivedStatus);
    const resolved = rows.filter(i => i.paper_status === 'CLOSED' || i.r_3d != null || i.r_10d != null);
    const active = rows.filter(i => i.paper_status === 'OPEN');
    const rValues = resolved.map(i => i.r_3d ?? i.r_10d ?? i.r_1d).filter(v => v != null);
    const wins = resolved.filter(i => i.paper_outcome === 'WIN' || i.hit === 'TARGET' || (i.r_3d != null && i.r_3d > 0));
    const best = rows.filter(i => i.r_3d != null).sort((a,b)=>b.r_3d-a.r_3d)[0] || null;
    const worst = rows.filter(i => i.r_3d != null).sort((a,b)=>a.r_3d-b.r_3d)[0] || null;
    const avg = rValues.length ? rValues.reduce((a,b)=>a+b,0)/rValues.length : null;
    res.json({
      ok: true,
      totalIdeas: rows.length,
      activeCount: active.length,
      resolvedCount: resolved.length,
      percentResolved: rows.length ? Math.round((resolved.length / rows.length) * 100) : 0,
      winRate: resolved.length ? Math.round((wins.length / resolved.length) * 100) : null,
      avgR: avg == null ? null : Number(avg.toFixed(3)),
      bestTrade: best ? { date: best.date, ticker: best.ticker, r_3d: best.r_3d, hit: best.hit } : null,
      worstTrade: worst ? { date: worst.date, ticker: worst.ticker, r_3d: worst.r_3d, hit: worst.hit } : null,
    });
  });

  app.get('/api/score/statistics', adminOnly, (req, res) => {
    try {
      res.json(statisticsPayload(req));
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/run-metadata', async (req, res) => {
    try {
      ensure();
      const body = req.body || {};
      if (!body.date) return res.status(400).json({ error: 'date required' });
      db.data.system2_run_metadata = db.data.system2_run_metadata.filter(r => r.date !== body.date);
      db.data.system2_run_metadata.push({
        date: body.date,
        created_at: body.created_at || now(),
        stages: body.stages || [],
        counts: body.counts || {},
        stage1_reject_counts: body.stage1_reject_counts || {},
        stage1_breakdown: body.stage1_breakdown || {},
        stage3_news_safety: body.stage3_news_safety || {},
        stage4_options_verdict_counts: body.stage4_options_verdict_counts || {},
        chronos_inference_seconds: body.chronos_inference_seconds != null ? Number(body.chronos_inference_seconds) : null,
        kronos_inference_seconds: body.kronos_inference_seconds != null ? Number(body.kronos_inference_seconds) : null,
        total_forecast_seconds: body.total_forecast_seconds != null ? Number(body.total_forecast_seconds) : null,
        forecast_stage: body.forecast_stage || {},
        source_breakdown: body.source_breakdown || {},
        stage7_report: body.stage7_report || {},
        regime_checked_at: body.regime_checked_at || null,
        regime: body.regime || null,
        regime_reason: body.regime_reason || null,
        regime_aborted: body.regime_aborted === true,
        spy_1d_pct: body.spy_1d_pct != null ? Number(body.spy_1d_pct) : null,
        qqq_1d_pct: body.qqq_1d_pct != null ? Number(body.qqq_1d_pct) : null,
        vix_current: body.vix_current != null ? Number(body.vix_current) : null,
        vix_1d_chg: body.vix_1d_chg != null ? Number(body.vix_1d_chg) : null,
        near_miss_count: body.near_miss_count || 0,
        safety_filter_active: body.safety_filter_active === true,
        safety_filter_removed_count: body.safety_filter_removed_count || 0,
        selection_logic_changed: body.selection_logic_changed === true,
        paper_only: body.paper_only !== false,
      });
      const incoming = Array.isArray(body.rejections) ? body.rejections : [];
      db.data.system2_rejections = db.data.system2_rejections.filter(r => r.date !== body.date);
      db.data.system2_rejections.push(...incoming.map((r, idx) => ({
        id: `${body.date}_${r.ticker}_${r.stage_rejected}_${idx}`,
        logged_at: now(),
        scored_stage: r.scored_stage || 0,
        hit: r.hit || null,
        paper_status: r.paper_status || 'REJECTED',
        ...r,
      })));
      await db.write();
      res.json({ ok: true, date: body.date, stages: body.stages?.length || 0, rejections: incoming.length });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/run-metadata', adminOnly, (req, res) => {
    ensure();
    const rows = [...db.data.system2_run_metadata].sort((a,b)=>(b.date||'').localeCompare(a.date||''));
    const date = req.query.date || rows[0]?.date;
    const row = rows.find(r => r.date === date) || null;
    res.json({ ok: true, date, metadata: row, available_dates: rows.map(r => r.date) });
  });

  function normalizeStageKey(value) {
    const raw = String(value || '').trim().toLowerCase();
    const aliases = {
      '0': 'universe', universe: 'universe',
      '1': 'stage1', stage1: 'stage1', cheap: 'stage1',
      '2': 'stage2', stage2: 'stage2', technical: 'stage2',
      '3': 'stage3', stage3: 'stage3', options: 'stage3',
      '4': 'stage4', stage4: 'stage4', chronos: 'stage4',
      '5': 'stage5', stage5: 'stage5', news: 'stage5',
      '6': 'stage6', stage6: 'stage6', council: 'stage6',
      '7': 'stage7', stage7: 'stage7', correlation: 'stage7',
      finalists: 'finalists', final: 'finalists',
    };
    return aliases[raw] || raw;
  }

  app.post('/api/system2/stage-detail', localOnly, async (req, res) => {
    try {
      ensure();
      const body = req.body || {};
      if (!body.date) return res.status(400).json({ error: 'date required' });
      const stages = Array.isArray(body.stages) ? body.stages : [];
      db.data.system2_stage_details = db.data.system2_stage_details.filter(r => r.date !== body.date);
      db.data.system2_stage_details.push({
        date: body.date,
        created_at: body.created_at || now(),
        paper_only: body.paper_only !== false,
        selection_logic_changed: body.selection_logic_changed === true,
        stages,
      });
      await db.write();
      res.json({ ok: true, date: body.date, stages: stages.length, tickers: stages.reduce((sum, s) => sum + ((s.tickers || []).length), 0) });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/stage-detail', adminOnly, (req, res) => {
    ensure();
    const rows = [...db.data.system2_stage_details].sort((a,b)=>(b.date||'').localeCompare(a.date||''));
    const date = req.query.date || rows[0]?.date;
    const row = rows.find(r => r.date === date) || null;
    if (!row) return res.json({ ok: true, date, available_dates: rows.map(r => r.date), stages: [], stage: null });
    const enrichStage = (stage) => {
      if (!stage || normalizeStageKey(stage.stage_key || stage.stage) !== 'finalists') return stage;
      const ideasByTicker = Object.fromEntries(
        db.data.ideas
          .filter(i => i.date === date)
          .map(i => [String(i.ticker).toUpperCase(), withDerivedStatus(i)])
      );
      return {
        ...stage,
        tickers: (stage.tickers || []).map(r => {
          const latest = ideasByTicker[String(r.ticker).toUpperCase()] || {};
          return { ...r, data: { ...(r.data || {}), ...latest } };
        }),
      };
    };
    const wanted = req.query.stage ? normalizeStageKey(req.query.stage) : null;
    if (wanted) {
      const stage = (row.stages || []).find(s => normalizeStageKey(s.stage_key || s.stage) === wanted) || null;
      const enriched = enrichStage(stage);
      return res.json({ ok: true, date, available_dates: rows.map(r => r.date), stage: enriched, stages: enriched ? [enriched] : [] });
    }
    res.json({ ok: true, date, available_dates: rows.map(r => r.date), stages: (row.stages || []).map(enrichStage) });
  });

  function sendSystem2Rejections(req, res) {
    ensure();
    const { date, stage, ticker, near_miss, limit = 1000 } = req.query;
    let rows = [...db.data.system2_rejections].reverse();
    if (date) rows = rows.filter(r => r.date === date);
    if (stage) rows = rows.filter(r => r.stage_rejected === stage);
    if (ticker) rows = rows.filter(r => String(r.ticker).toUpperCase() === String(ticker).toUpperCase());
    if (near_miss != null) rows = rows.filter(r => String(Boolean(r.near_miss)) === String(near_miss));
    res.json({ ok: true, rejections: rows.slice(0, parseInt(limit)), total: rows.length });
  }

  app.get('/api/system2/rejections', adminOnly, sendSystem2Rejections);
  app.get('/api/rejections/system2', adminOnly, sendSystem2Rejections);

  app.get('/api/system2/monitor', adminOnly, (req, res) => {
    ensure();
    const active = db.data.ideas.map(withDerivedStatus)
      .filter(i => i.paper !== false && i.paper_status === 'OPEN')
      .map(i => ({
        id: i.id,
        date: i.date,
        ticker: i.ticker,
        source: i.source || 'scanner',
        entry: i.entry,
        stop: i.stop,
        target: i.target,
        current_price: i.current_price,
        unrealized_r: i.unrealized_r,
        current_gain_pct: i.current_gain_pct,
        distance_to_target_pct: i.distance_to_target_pct,
        distance_to_stop_pct: i.distance_to_stop_pct,
        peak_gain_pct: i.monitor_peak_gain_pct,
        worst_dd_pct: i.monitor_worst_dd_pct,
        paper_exit_reason: i.paper_exit_reason,
        paper_exit_at: i.paper_exit_at,
        monitor_updated_at: i.monitor_updated_at,
      }));
    res.json({
      ok: true,
      active,
      activeCount: active.length,
      latestSnapshots: db.data.system2_monitor_snapshots.slice(-200).reverse(),
    });
  });

  app.post('/api/system2/monitor/run', adminOnly, async (req, res) => {
    try {
      res.json(await runPaperMonitor());
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/monitor/run-local', localOnly, async (req, res) => {
    try {
      res.json(await runPaperMonitor());
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/pre-market-gap/run', adminOnly, async (req, res) => {
    try {
      res.json(await runPreMarketGapCheck(req.body || {}));
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.post('/api/system2/pre-market-gap/run-local', localOnly, async (req, res) => {
    try {
      res.json(await runPreMarketGapCheck(req.body || {}));
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/config', adminOnly, (req, res) => {
    res.json({ ok: true, config: readSystem2Config(), path: configPath });
  });

  app.post('/api/system2/config', adminOnly, (req, res) => {
    try {
      const next = deepMerge(readSystem2Config(), req.body || {});
      fs.mkdirSync(system2Root, { recursive: true });
      fs.writeFileSync(configPath, JSON.stringify(next, null, 2));
      res.json({ ok: true, config: next, path: configPath });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/status', adminOnly, (req, res) => {
    try {
      ensure();
      const latestRun = latestRunSummary();
      const cron = (() => {
        try { return childProcess.execSync('crontab -l', { encoding: 'utf8', timeout: 5000 }); }
        catch { return ''; }
      })();
      res.json({
        ok: true,
        mode: 'paper',
        coreDir: system2Root,
        config: readSystem2Config(),
        cron,
        latestRun,
        runMetadataDates: [...db.data.system2_run_metadata].map(r => r.date).sort().reverse(),
        rejectionCount: db.data.system2_rejections.length,
        monitorSnapshotCount: db.data.system2_monitor_snapshots.length,
        activePaperIdeas: db.data.ideas.map(withDerivedStatus).filter(i => i.paper !== false && i.paper_status === 'OPEN').length,
        nightlyLogTail: lastLines(path.join(logDir, 'nightly.log'), 120),
        artifacts: {
          universe: countArtifact('universe.json'),
          stage1Survivors: countArtifact('stage1_survivors.json'),
          stage2Top40: countArtifact('stage2_surgical_strike_top40.json'),
          stage5CombinedForecastTop40: countArtifact('stage5_combined_forecast_top40.json'),
          stage3NewsSafeTop40: countArtifact('stage3_news_safe_top40.json'),
          stage4OptionsTop40: countArtifact('stage4_options_enriched_top40.json'),
          confluenceTop40: countArtifact('stage2_confluence_ranked_top40.json'),
          stage7Survivors: countArtifact('stage7_clustered_survivors.json'),
          stage7Report: countArtifact('stage7_cluster_report.json'),
          ideaLog: countArtifact('phase_b_baseline_idea_log_results.json'),
        },
      });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/system2/artifacts/:name', adminOnly, (req, res) => {
    const allowed = new Set([
      'universe.json',
      'stage1_metadata.json',
      'stage1_details.json',
      'stage1_survivors.json',
      'stage2_surgical_strike_metadata.json',
      'stage2_surgical_strike_scored.json',
      'stage2_surgical_strike_top40.json',
      'stage4_chronos_metadata.json',
      'stage4_chronos_enriched_top40.json',
      'stage5_combined_forecast_metadata.json',
      'stage5_combined_forecast_top40.json',
      'stage3_news_metadata.json',
      'stage3_news_safe_top40.json',
      'stage3_news_rejections.json',
      'stage4_options_metadata.json',
      'stage4_options_enriched_top40.json',
      'stage2_confluence_metadata.json',
      'stage2_confluence_ranked_top40.json',
      'stage7_cluster_report.json',
      'stage7_clustered_survivors.json',
      'stage7_cluster_rejections.json',
      'phase_b_baseline_idea_log_results.json',
    ]);
    if (!allowed.has(req.params.name)) return res.status(400).json({ error: 'Artifact not allowed' });
    const file = path.join(system2Root, req.params.name);
    const data = readJson(file, null);
    if (data == null) return res.status(404).json({ error: 'Artifact not found' });
    res.json({ ok: true, name: req.params.name, data });
  });

  app.post('/api/system2/run', adminOnly, (req, res) => {
    try {
      const child = childProcess.spawn('/bin/bash', [path.join(system2Root, 'run_phase_b_core_baseline.sh')], {
        cwd: system2Root,
        detached: true,
        stdio: 'ignore',
      });
      child.unref();
      res.json({ ok: true, started: true, message: 'Phase B baseline runner started in background.' });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });
};
