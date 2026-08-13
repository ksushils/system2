// ════════════════════════════════════════════════════════════
// TELEMETRY GAP DETECTOR
//
// pa ran 170 trading-path executions yesterday and recorded ZERO
// decisions of any kind. Its run data held 153 skip reasons --
// VOLUME_NOT_TAPERING, NO_PATTERN, NO_FLAGPOLE, BELOW_VWAP -- none of
// which reached the database, because pa has no Rejection Funnel node
// where indices and crypto do. That went unnoticed for two months, and
// every scoreboard row for pa, comm and fmp_alpaca has been
// uninterpretable the whole time.
//
// The signal is NOT "few rejections". A scanner may legitimately reject
// nothing on a quiet day, and firing on that would make the warning
// noise. The signal is ACTING WITHOUT RECORDING: trading-path
// executions above a threshold, with no signal, no rejection and no
// trade to show for them.
//
// A scanner that is idle, out of hours, or not running at all cannot
// raise this -- its trading path never fired, so there is nothing it
// should have recorded.
// ════════════════════════════════════════════════════════════
import { execFile } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import pg from 'pg';

const HERE = path.dirname(fileURLToPath(import.meta.url));

let pool = null;
function getPool() {
  if (pool) return pool;
  if (!process.env.DATABASE_URL) return null;
  pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 2 });
  return pool;
}

// Enough executions that silence is meaningful. pa ran 170; a handful of
// runs proves nothing either way.
export const TRADING_PATH_THRESHOLD = Number(process.env.TELEMETRY_GAP_MIN_EXEC || 20);
// Reading the n8n sqlite is expensive and /api/health is called ~550x a
// day, so this is cached rather than computed per request.
const CACHE_TTL_MS = 10 * 60_000;
let cache = { data: null, at: 0 };

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

export function isWeekendUTC(now = new Date()) {
  const d = now.getUTCDay();
  return d === 0 || d === 6;
}

// Pure, so the decision is testable without a database or n8n.
// Every counter here is a ROLLING 24-HOUR total, never "today". The
// names say so, because these render beside per-day scoreboard rows and
// the ambiguity has already produced one wrong conclusion: a reader saw
// "3 trades" against volume and took it for today's, when all three were
// the previous day's.
export function classify({ scanner, active, trading_path_24h, signals_24h, rejections_24h, trades_24h,
                           threshold = TRADING_PATH_THRESHOLD, weekend = false }) {
  const recorded_24h = signals_24h + rejections_24h + trades_24h;
  const tradingPath = trading_path_24h;
  if (!active) return { scanner, gap: false, reason: 'workflow not active' };
  if (weekend) return { scanner, gap: false, reason: 'weekend — sessions do not run' };
  // The threshold IS the window test. A scanner outside its hours does
  // not reach its trading path, so it cannot accumulate executions here
  // -- no separate "is it in window now" check is needed, and adding one
  // suppressed the very cases this exists to catch (pa ran 170 times
  // yesterday; at 11:10 today its bucket was empty and it read as
  // out-of-window).
  if (tradingPath < threshold) {
    return { scanner, gap: false, trading_path_24h,
             reason: `only ${tradingPath} trading-path executions in 24h (threshold ${threshold}) — its trading path is not running, which is a different defect from a telemetry gap` };
  }
  if (recorded_24h === 0) {
    return {
      scanner, gap: true, gap_type: 'NO_DECISIONS_RECORDED',
      trading_path_24h, recorded_24h: 0, signals_24h, rejections_24h, trades_24h,
      reason: `${tradingPath} trading-path executions in 24h and NOT ONE recorded decision — the scanner is acting without recording. "0 rejections" here means unmeasured, not none.`
    };
  }
  // Partial gap: signals were emitted, but not one of them ended in a
  // trade OR a rejection. Every candidate vanished without a recorded
  // outcome. This is NOT inferred from the rejection count alone -- a
  // scanner that rejects nothing and trades nothing should also emit no
  // signals. pa emitted 250 and its 153 skip decisions reached nothing.
  if (signals_24h > 0 && trades_24h === 0 && rejections_24h === 0) {
    return {
      scanner, gap: true, gap_type: 'SIGNALS_WITHOUT_OUTCOME',
      trading_path_24h, recorded_24h, signals_24h, rejections_24h, trades_24h,
      reason: `${signals_24h} signals in 24h but ZERO trades and ZERO rejections — every candidate vanished without a recorded outcome. The skip decisions exist in run data and reach no table.`
    };
  }
  return { scanner, gap: false, trading_path_24h, recorded_24h,
           reason: `recording normally in the last 24h (${signals_24h} signals, ${rejections_24h} rejections, ${trades_24h} trades)` };
}

export async function telemetryGaps({ force = false, now = new Date() } = {}) {
  if (!force && cache.data && Date.now() - cache.at < CACHE_TTL_MS) return cache.data;
  const pgp = getPool();
  if (!pgp) return { checked_at: new Date().toISOString(), gaps: [], scanners: [], error: 'no database' };

  const [execs, counts, recent] = await Promise.all([
    runExecReader(1),
    pgp.query(`
      SELECT s.scanner,
             coalesce(sig.n,0)::int AS signals,
             coalesce(rej.n,0)::int AS rejections,
             coalesce(tr.n,0)::int  AS trades
      FROM (SELECT DISTINCT scanner FROM signals WHERE created_at > now()-interval '24 hours'
            UNION SELECT DISTINCT scanner FROM rejections WHERE created_at > now()-interval '24 hours'
            UNION SELECT DISTINCT scanner FROM trades WHERE opened_at > now()-interval '24 hours') s
      LEFT JOIN (SELECT scanner, count(*) n FROM signals    WHERE created_at > now()-interval '24 hours' GROUP BY 1) sig ON sig.scanner=s.scanner
      LEFT JOIN (SELECT scanner, count(*) n FROM rejections WHERE created_at > now()-interval '24 hours' GROUP BY 1) rej ON rej.scanner=s.scanner
      LEFT JOIN (SELECT scanner, count(*) n FROM trades     WHERE opened_at  > now()-interval '24 hours' GROUP BY 1) tr  ON tr.scanner=s.scanner`),
    Promise.resolve(null)
  ]);

  const byScanner = new Map();
  for (const r of counts.rows) byScanner.set(r.scanner, r);

  const weekend = isWeekendUTC(now);
  const results = [];
  for (const [scanner, meta] of Object.entries(execs.scanners || {})) {
    const days = meta.days || {};
    const trading_path_24h = Object.values(days).reduce((s, d) => s + (d.trading_path || 0), 0);
    const c = byScanner.get(scanner) || { signals: 0, rejections: 0, trades: 0 };
    results.push(classify({
      scanner, active: meta.active, trading_path_24h,
      signals_24h: c.signals, rejections_24h: c.rejections, trades_24h: c.trades, weekend
    }));
  }

  const data = {
    checked_at: new Date().toISOString(),
    threshold: TRADING_PATH_THRESHOLD,
    weekend,
    exec_reader_error: execs.error ?? null,
    gaps: results.filter(r => r.gap).map(r => r.scanner),
    window: 'rolling 24 hours',
    scanners: results.sort((a, b) => (b.trading_path_24h || 0) - (a.trading_path_24h || 0))
  };
  cache = { data, at: Date.now() };
  return data;
}

export function telemetryGapCacheAge() {
  return cache.data ? Math.round((Date.now() - cache.at) / 1000) : null;
}
