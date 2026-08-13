// ════════════════════════════════════════════════════════════
// PROVING-WINDOW SCOREBOARD
//
// Built so a few days of live trading produce a verdict-shaped record
// instead of scattered rows. The hard part is not the counting -- it is
// refusing to let a number look like evidence when it is not.
//
// Every scanner row therefore carries n and, where n is small,
// verdict_available:false with the reason spelled out. At current
// volumes that is nearly every row: indices has 12 closed trades across
// three days. A bare P&L figure next to n=12 invites a conclusion the
// data cannot support, so the refusal travels with the number.
//
// Also refused: blending adopted trades (source='adopted_untracked')
// into a scanner's P&L. Those were placed by fmp, never gated, and
// adopted after the fact -- real money, but not evidence about fmp's
// edge. They are counted separately and flagged.
//
// Carry is reported per closed trade as a share of that trade's risk,
// because it is not a rounding error here: indices' financing is 39.1%
// of |gross|, and one US100 winner paid 23.7% of its gain in carry.
// ════════════════════════════════════════════════════════════
import { execFile } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import pg from 'pg';
import { telemetryGaps } from './telemetry-gap.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));

// Lazy: ESM imports evaluate before dotenv.config() in the index.js body.
let pool = null;
function getPool() {
  if (pool) return pool;
  if (!process.env.DATABASE_URL) return null;
  pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 3 });
  return pool;
}

// n>=30 is the system's discipline device. Below it a row describes what
// happened; it does not estimate an edge.
export const MIN_N_FOR_VERDICT = 30;

// fmp and fmp_alpaca run a byte-identical 47,000-character signal engine
// (FMP Signal Engine v13; similarity 1.00 across 65 of 74 nodes). Two rows
// of the SAME experiment. Counting them as independent evidence doubles an
// apparent sample that has not grown, so every surface that reports them
// must say so.
export const SAME_STRATEGY_AS = { fmp: 'fmp_alpaca', fmp_alpaca: 'fmp' };

export function verdictFor(n, extra = {}) {
  if (n >= MIN_N_FOR_VERDICT) return { verdict_available: true, n };
  const reasons = [`n=${n} is below the ${MIN_N_FOR_VERDICT}-trade minimum`];
  if (extra.days) reasons.push(`across only ${extra.days} trading day(s)`);
  if (extra.connected === 0) reasons.push('and none of this scanner\'s parameters are read at runtime, so results cannot be attributed to any configuration');
  if (extra.coverage != null && extra.coverage < 50) reasons.push(`outcome labelling covers only ${extra.coverage}% of its events`);
  return { verdict_available: false, n, reason: reasons.join('; ') };
}

function runExecReader(days, timeoutMs = 120_000) {
  return new Promise(resolve => {
    const child = execFile('python3', [path.join(HERE, 'scoreboard-execs.py')],
      { timeout: timeoutMs, maxBuffer: 32 * 1024 * 1024 },
      (err, stdout) => {
        if (err) return resolve({ scanners: {}, error: err.message });
        try { resolve(JSON.parse(stdout)); } catch (e) { resolve({ scanners: {}, error: e.message }); }
      });
    child.stdin.end(JSON.stringify({ days }));
  });
}

export async function buildScoreboard({ days = 7, connectivity = null } = {}) {
  const pg = getPool();
  if (!pg) throw new Error('Postgres is required for the scoreboard');
  const since = `${days} days`;

  const [signals, rejections, trades, skips, changelog, execs] = await Promise.all([
    pg.query(`SELECT scanner, created_at::date AS d, count(*)::int n
              FROM signals WHERE created_at >= now() - $1::interval GROUP BY 1,2`, [since]),
    pg.query(`SELECT scanner, created_at::date AS d, count(*)::int n
              FROM rejections WHERE created_at >= now() - $1::interval GROUP BY 1,2`, [since]),
    pg.query(`SELECT id, scanner, ticker, status, opened_at, closed_at,
                     entry, sl, close_price, pnl_gross, pnl_net, financing_accrued,
                     risk_amount, mae_r, mfe_r,
                     data->>'source' AS source, data->>'size' AS size,
                     data->>'risk_usd' AS risk_usd, data->>'close_source' AS close_source,
                     data->>'financing_source' AS financing_source
              FROM trades
              WHERE opened_at >= now() - $1::interval OR closed_at >= now() - $1::interval`, [since]),
    pg.query(`SELECT scanner, reason, count(*)::int n FROM outcome_label_skips GROUP BY 1,2`),
    pg.query(`SELECT scanner, parameter, old_value, new_value, date::date AS d
              FROM intelligence_changelog WHERE date >= now() - $1::interval
                AND parameter NOT LIKE 'REJECTED:%' ORDER BY date`, [since]),
    runExecReader(days)
  ]);

  const bucket = new Map();   // `${scanner}|${day}` -> row
  const row = (scanner, d) => {
    const k = `${scanner}|${d}`;
    if (!bucket.has(k)) bucket.set(k, {
      scanner, date: d,
      executions: 0, trading_path_executions: 0, heartbeat_only_executions: 0,
      signals: 0, rejections: 0,
      orders_placed: 0, trades_opened: 0, trades_closed: 0,
      pnl_gross: 0, carry: 0, pnl_net: 0, pnl_r: null,
      mae_r: [], mfe_r: [],
      adopted_opened: 0, adopted_closed: 0, adopted_pnl_net: 0,
      params_changed: []
    });
    return bucket.get(k);
  };

  for (const r of signals.rows) row(r.scanner, r.d.toISOString().slice(0, 10)).signals = r.n;
  for (const r of rejections.rows) row(r.scanner, r.d.toISOString().slice(0, 10)).rejections = r.n;

  for (const s of Object.keys(execs.scanners || {})) {
    for (const [d, v] of Object.entries(execs.scanners[s].days || {})) {
      const t = row(s, d);
      t.executions = v.executions;
      t.trading_path_executions = v.trading_path;
      t.heartbeat_only_executions = v.heartbeat_only;
    }
  }

  for (const c of changelog.rows) {
    row(c.scanner, c.d.toISOString().slice(0, 10)).params_changed.push(
      { parameter: c.parameter, from: c.old_value, to: c.new_value });
  }

  // Per-trade economics. Adopted rows are counted but NEVER blended.
  // ONE definition of the accumulator shape. It was declared in two places and
  // the second gained netSum/grossSum/carrySum while the first did not, so every
  // pre-seeded active scanner accumulated `undefined + n` = NaN and reported it
  // as a P&L. Both sites call this now.
  const newScannerAcc = () => ({
    closed: 0, adopted_closed: 0, days: new Set(), rSum: 0, rN: 0,
    netSum: 0, grossSum: 0, carrySum: 0,
    // winners only: a sum of positive terms, so it cannot cancel to near zero
    grossProfit: 0, grossLoss: 0,
    intraday:  { n: 0, gross: 0, carry: 0, net: 0 },
    overnight: { n: 0, gross: 0, carry: 0, net: 0 },
    sessionUnknown: 0
  });
  const perScanner = new Map();
  // Seed from every ACTIVE scanner first. Building this map from trades
  // alone meant a scanner with none had no row at all -- pa and
  // fmp_alpaca, the two carrying telemetry gaps, were invisible on the
  // very board meant to surface them. A missing row and a zero row make
  // different claims, and only one of them is honest here.
  for (const [s, meta] of Object.entries(execs.scanners || {})) {
    if (meta.active) perScanner.set(s, newScannerAcc());
  }
  const carryRows = [];
  for (const t of trades.rows) {
    const adopted = t.source === 'adopted_untracked';
    const openedD = t.opened_at ? t.opened_at.toISOString().slice(0, 10) : null;
    const closedD = t.closed_at ? t.closed_at.toISOString().slice(0, 10) : null;

    if (openedD) {
      const r = row(t.scanner, openedD);
      if (adopted) r.adopted_opened++; else { r.trades_opened++; r.orders_placed++; }
    }
    if (closedD) {
      const r = row(t.scanner, closedD);
      const net = t.pnl_net == null ? null : Number(t.pnl_net);
      if (adopted) {
        r.adopted_closed++;
        if (net != null) r.adopted_pnl_net += net;
      } else {
        r.trades_closed++;
        if (net != null) {
          r.pnl_gross += Number(t.pnl_gross ?? 0);
          r.carry += Number(t.financing_accrued ?? 0);
          r.pnl_net += net;
        }
      }
      if (t.mae_r != null) r.mae_r.push(Number(t.mae_r));
      if (t.mfe_r != null) r.mfe_r.push(Number(t.mfe_r));
    }

    // Realised risk from the actual fill and stop -- the intent figure
    // (risk_usd) is what was planned, not what was carried.
    const realisedRisk = (t.entry != null && t.sl != null && t.size != null)
      ? Math.abs(Number(t.entry) - Number(t.sl)) * Number(t.size) : null;

    if (t.closed_at && t.pnl_net != null) {
      const carry = Number(t.financing_accrued ?? 0);
      carryRows.push({
        trade_id: t.id, scanner: t.scanner, ticker: t.ticker,
        closed_date: closedD,
        adopted, carry,
        // ACTUAL where Capital's own rate produced it; MODELLED otherwise.
        carry_basis: carry === 0 ? 'NONE_INTRADAY'
          : (t.financing_source ? `ACTUAL (${t.financing_source})` : 'MODELLED'),
        risk: realisedRisk,
        carry_pct_of_risk: realisedRisk ? +(100 * carry / realisedRisk).toFixed(1) : null,
        pnl_gross: t.pnl_gross == null ? null : Number(t.pnl_gross),
        pnl_net: Number(t.pnl_net),
        pnl_r: realisedRisk ? +(Number(t.pnl_net) / realisedRisk).toFixed(3) : null,
        close_source: t.close_source ?? null
      });
    }

    if (!perScanner.has(t.scanner)) perScanner.set(t.scanner, newScannerAcc());
    const ps = perScanner.get(t.scanner);
    if (t.closed_at && t.pnl_net != null) {
      if (adopted) ps.adopted_closed++;
      else {
        ps.closed++;
        // net/gross/carry follow the same exclusion as closed and R: adopted
        // trades are not this scanner's result.
        const netV   = Number(t.pnl_net);
        const grossV = Number(t.pnl_gross ?? t.pnl_net ?? 0);
        const carryV = Number(t.financing_accrued ?? 0);
        ps.netSum   += netV;
        ps.grossSum += grossV;
        ps.carrySum += carryV;
        if (grossV > 0) ps.grossProfit += grossV; else ps.grossLoss += Math.abs(grossV);
        // Overnight = the UTC date changed between fill and close, matching the
        // query that first separated these. A trade held 23h inside one UTC day
        // pays no financing; one held 20 minutes across midnight does.
        const oD = t.opened_at ? new Date(t.opened_at) : null;
        const cD = t.closed_at ? new Date(t.closed_at) : null;
        const datesOk = oD && cD && !Number.isNaN(oD.getTime()) && !Number.isNaN(cD.getTime());
        if (!datesOk) ps.sessionUnknown++;
        else {
          const b = (oD.toISOString().slice(0, 10) !== cD.toISOString().slice(0, 10))
            ? ps.overnight : ps.intraday;
          b.n++; b.gross += grossV; b.carry += carryV; b.net += netV;
        }
        if (realisedRisk) { ps.rSum += Number(t.pnl_net) / realisedRisk; ps.rN++; }
      }
    }
    if (openedD) ps.days.add(openedD);
  }

  // Per-day R, from THAT DAY's own trades. Filtering by scanner alone
  // smears one total across every row the scanner appears in, including
  // days it did not trade -- which is how a day with no closes ends up
  // showing an R figure.
  for (const r of bucket.values()) {
    const rs = carryRows.filter(c =>
      c.scanner === r.scanner && c.closed_date === r.date && !c.adopted && c.pnl_r != null);
    r.pnl_r = rs.length ? +(rs.reduce((s, c) => s + c.pnl_r, 0)).toFixed(3) : null;
    r.mae_r = r.mae_r.length ? +(r.mae_r.reduce((a, b) => a + b, 0) / r.mae_r.length).toFixed(3) : null;
    r.mfe_r = r.mfe_r.length ? +(r.mfe_r.reduce((a, b) => a + b, 0) / r.mfe_r.length).toFixed(3) : null;
  }

  // Outcome coverage: computed over successes AND failures. Coverage over
  // successes alone is survivorship bias -- the skip table must be counted.
  const skipsBy = new Map();
  for (const s of skips.rows) {
    if (!skipsBy.has(s.scanner)) skipsBy.set(s.scanner, { total: 0, reasons: {} });
    const e = skipsBy.get(s.scanner);
    e.total += s.n; e.reasons[s.reason] = s.n;
  }

  // Beside the zeros, so "0 rejections" and "unmeasured" cannot be
  // mistaken for one another on the same row.
  let gapBy = {};
  try {
    const tg = await telemetryGaps();
    for (const r of tg.scanners || []) gapBy[r.scanner] = {
      gap: r.gap, gap_type: r.gap_type ?? null, reason: r.reason,
      window: 'rolling 24 hours',
      trading_path_24h: r.trading_path_24h ?? 0,
      signals_24h: r.signals_24h ?? null, rejections_24h: r.rejections_24h ?? null,
      trades_24h: r.trades_24h ?? null };
  } catch { /* scoreboard must render without it */ }

  const connBy = {};
  if (connectivity?.by_scanner) for (const [s, v] of Object.entries(connectivity.by_scanner)) connBy[s] = v;

  const scanners = {};
  for (const [s, ps] of perScanner.entries()) {
    const tradingDays = ps.days.size || 1;
    const rate = +(ps.closed / tradingDays).toFixed(2);
    const conn = connBy[s] || null;
    const sk = skipsBy.get(s) || null;
    const evSignals = signals.rows.filter(r => r.scanner === s).reduce((a, b) => a + b.n, 0);
    const evRej = rejections.rows.filter(r => r.scanner === s).reduce((a, b) => a + b.n, 0);
    const labelled = evSignals + evRej;
    const coverage = labelled + (sk?.total ?? 0) > 0
      ? +(100 * labelled / (labelled + (sk?.total ?? 0))).toFixed(1) : null;

    scanners[s] = {
      closed_trades: ps.closed,
      adopted_closed: ps.adopted_closed,
      trading_days_seen: ps.days.size,
      trades_per_trading_day: rate,
      trading_days_to_n30: rate > 0 ? Math.ceil((MIN_N_FOR_VERDICT - ps.closed) / rate) : null,
      total_r: ps.rN ? +ps.rSum.toFixed(3) : null,
      net_pnl: ps.closed ? +ps.netSum.toFixed(2) : null,
      gross_pnl: ps.closed ? +ps.grossSum.toFixed(2) : null,
      carry_total: ps.closed ? +ps.carrySum.toFixed(2) : null,
      gross_profit: ps.closed ? +ps.grossProfit.toFixed(2) : null,
      gross_loss: ps.closed ? +ps.grossLoss.toFixed(2) : null,
      // Denominator is the WINNERS' gross profit -- a sum of positive terms,
      // so it cannot cancel. The previous metric divided by |sum(gross)| and
      // read 247.8% for indices, where +1,742 overnight and -1,603 intraday
      // very nearly cancelled to +138.62 against 343.50 of carry. That implied
      // carry was destroying the edge when the overnight trades in fact netted
      // +1,426 AFTER paying all of it.
      carry_pct_of_gross_profit: ps.grossProfit ? +(100 * ps.carrySum / ps.grossProfit).toFixed(1) : null,
      // Second line of defence: if carry is larger than the NET gross, any
      // ratio against that net is untrustworthy. Say so and quote the
      // absolute instead of letting the old shape of mistake recur silently.
      carry_note: (ps.closed && Math.abs(ps.grossSum) < Math.abs(ps.carrySum))
        ? `net gross ${ps.grossSum.toFixed(2)} is smaller than carry ${ps.carrySum.toFixed(2)} — a ratio to net gross would mislead; carry is ${ps.carrySum.toFixed(2)} absolute`
        : null,
      session_split: ps.closed ? {
        intraday:  { n: ps.intraday.n,  gross: +ps.intraday.gross.toFixed(2),
                     carry: +ps.intraday.carry.toFixed(2),  net: +ps.intraday.net.toFixed(2) },
        overnight: { n: ps.overnight.n, gross: +ps.overnight.gross.toFixed(2),
                     carry: +ps.overnight.carry.toFixed(2), net: +ps.overnight.net.toFixed(2) },
        undated: ps.sessionUnknown
      } : null,
      same_strategy_as: SAME_STRATEGY_AS[s] ?? null,
      ...verdictFor(ps.closed, { days: ps.days.size, connected: conn?.CONNECTED, coverage }),
      param_connectivity: conn ? {
        connected: conn.CONNECTED, no_runtime_read: conn.NO_RUNTIME_READ,
        unproven: conn.UNPROVEN, not_wired: conn.NOT_WIRED, total: conn.total,
        note: conn.CONNECTED === 0
          ? 'no parameter is read at runtime — results cannot be attributed to any parameter set'
          : null
      } : null,
      telemetry_gap: gapBy[s] ?? null,
      outcome_coverage_pct: coverage,
      outcome_skips: sk ? { total: sk.total, top_reasons: sk.reasons } : null
    };
  }

  // Ensure every active scanner has at least one row, so "no activity"
  // renders as zeros rather than as absence.
  const todayKey = new Date().toISOString().slice(0, 10);
  for (const s of perScanner.keys()) {
    const has = [...bucket.values()].some(r => r.scanner === s);
    if (!has) row(s, todayKey);
  }

  const rows = [...bucket.values()].sort((a, b) => b.date.localeCompare(a.date) || a.scanner.localeCompare(b.scanner));
  // (setup_type, engine_branch) pairs. A breakdown, not a regrouping: nothing
  // that currently keys on setup_type changes behaviour, and rows with no
  // branch report engine_branch null rather than being dropped.
  const bySetupBranch = {};
  for (const t of trades) {
    if (!t.closed_at || t.pnl_net == null) continue;
    const key = `${t.setup_type || 'UNKNOWN'}|${t.engine_branch || 'null'}`;
    const e = bySetupBranch[key] || (bySetupBranch[key] = {
      scanner: t.scanner || null, setup_type: t.setup_type || 'UNKNOWN',
      engine_branch: t.engine_branch ?? null, n: 0, net: 0 });
    e.n++; e.net = +(e.net + Number(t.pnl_net || 0)).toFixed(2);
  }

  return {
    generated_at: new Date().toISOString(),
    window_days: days,
    min_n_for_verdict: MIN_N_FOR_VERDICT,
    exec_reader_error: execs.error ?? null,
    rows,
    scanners,
    by_setup_branch: Object.values(bySetupBranch).sort((a, b) => b.n - a.n),
    carry_detail: carryRows.sort((a, b) => (b.carry ?? 0) - (a.carry ?? 0)),
    notes: [
      'trading_path_executions is detected from response-side field names the scanners emit, not request URLs; URLs are built from variables and never appear literally in run data.',
      'adopted trades (source=adopted_untracked) are counted separately and never blended into a scanner P&L — they were never gated.',
      'pnl_r uses REALISED risk |entry-sl|*size from the actual fill, not the risk_usd intent figure.',
      'outcome coverage counts the skip table as well as successes; coverage over successes alone is survivorship bias.'
    ]
  };
}

// PURE. Takes a scoreboard object, returns lines. No I/O, no Telegram, so it
// can be verified directly instead of by firing runDailyDigest.
export function scannerDigestLines(sb) {
  const names = Object.keys(sb?.scanners || {}).sort();
  if (!names.length) return ['  (no scanner rows)'];
  const out = [];
  for (const s of names) {
    const v = sb.scanners[s] || {};
    const num = (x, suffix = '') => (x == null ? 'n/a' : `${x}${suffix}`);
    const bits = [
      `n=${v.closed_trades ?? 0}`,
      `net=${num(v.net_pnl)}`,
      `R=${num(v.total_r)}`,
      `n30 ${v.trading_days_to_n30 == null ? 'not at this rate' : `~${v.trading_days_to_n30}d`}`,
      `carry ${num(v.carry_pct_of_gross_profit, '% of gross profit')}`,
      `cover ${num(v.outcome_coverage_pct, '%')}`
    ];
    if (v.telemetry_gap?.gap) bits.push(`TELEMETRY_GAP:${v.telemetry_gap.gap_type || 'yes'}`);
    out.push(`  ${String(s).padEnd(12)} ${bits.join(' | ')}`);
    // The split is what makes a carry figure interpretable: intraday trades
    // pay almost nothing, so a blended number hides which half is costing.
    const sp = v.session_split;
    if (sp && (sp.intraday.n || sp.overnight.n)) {
      out.push(`      intraday  n=${sp.intraday.n} gross=${sp.intraday.gross} carry=${sp.intraday.carry} net=${sp.intraday.net}`);
      out.push(`      overnight n=${sp.overnight.n} gross=${sp.overnight.gross} carry=${sp.overnight.carry} net=${sp.overnight.net}`);
      if (sp.undated) out.push(`      undated   n=${sp.undated} (missing opened_at or closed_at)`);
    }
    if (v.carry_note) out.push(`      carry: ${v.carry_note}`);
    // The honesty fields must survive into the digest. A net without its n,
    // or without the reason no verdict is available, is exactly the failure
    // the scoreboard UI was built to prevent.
    if (v.verdict_available === false && v.reason) out.push(`      no verdict — ${v.reason}`);
    if (v.same_strategy_as) {
      out.push(`      SAME STRATEGY AS ${v.same_strategy_as} — byte-identical signal engine; NOT independent evidence`);
    }
    if (v.param_connectivity?.note) out.push(`      ${v.param_connectivity.note}`);
  }
  return out;
}

export function attachScoreboard(app, { adminOnly, computeParamConnectivity }) {
  app.get('/api/scoreboard/daily', adminOnly, async (req, res) => {
    try {
      const days = Math.max(1, Math.min(90, Number(req.query.days) || 7));
      let connectivity = null;
      try { connectivity = await computeParamConnectivity?.({ force: false }); } catch { /* optional */ }
      res.json(await buildScoreboard({ days, connectivity }));
    } catch (e) { res.status(500).json({ error: e.message }); }
  });
}
