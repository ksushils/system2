// ════════════════════════════════════════════════════════════
// PARAM CONNECTIVITY
//
// A control is only real if the scanner reads the value at runtime. The
// store holds 189 rows across 10 scanners; Block 3 established that only
// two scanners demonstrably read anything. A panel exposing all of them
// without that distinction would look authoritative and mostly do
// nothing.
//
// Status is derived from EXECUTED node data, never from workflow source.
// A param present in source but absent from executed data is exactly the
// case this exists to catch.
//
//   CONNECTED       the stored VALUE appears in run data.
//                   params_source:'server' alone is NOT sufficient -- that
//                   object carries no parameter values, so it evidences a
//                   fetch, not use. That is the mean_reversion failure mode.
//   NO_RUNTIME_READ zero appearances across a MEANINGFUL sample (>=30)
//                   BY A SCANNER THAT ACTUALLY RAN its trading path.
//   INDETERMINATE   enough executions, but none reached the trading path --
//                   halted, or out of window. Absence here is evidence
//                   about the scanner's state, not about the parameter.
//                   indices read CONNECTED=13 one day and 0 the next purely
//                   because its breaker short-circuited before the config
//                   node emitted anything. Both readings were honest; the
//                   badge was not.
//   UNPROVEN        sample thinner than that, or the name appears without
//                   the stored value. Absence of evidence is not evidence
//                   of absence and must not render as a negative.
//   NOT_WIRED       no active workflow declares this scanner.
//
// Scanning is expensive, so the result is cached with an as_of stamp and
// refreshed on demand.
// ════════════════════════════════════════════════════════════
import { execFile } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import pg from 'pg';

const HERE = path.dirname(fileURLToPath(import.meta.url));

// Lazy, like param-store.js:56. ESM imports are evaluated before the
// importing module's body runs, so reading process.env at module scope
// would sample it before dotenv.config() has populated it.
let pool = null;
function getPool() {
  if (pool) return pool;
  if (!process.env.DATABASE_URL) return null;
  pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 2 });
  return pool;
}

export const MEANINGFUL_SAMPLE = 30;
const CACHE_TTL_MS = 30 * 60_000;
let cache = { data: null, at: 0 };

function runReader(pairs, timeoutMs = 90_000) {
  return new Promise((resolve, reject) => {
    const child = execFile('python3', [path.join(HERE, 'param-connectivity.py')],
      { timeout: timeoutMs, maxBuffer: 32 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) return reject(new Error(`param-connectivity failed: ${err.message} ${String(stderr).slice(0, 200)}`));
        try { resolve(JSON.parse(stdout)); }
        catch (e) { reject(new Error(`param-connectivity returned non-JSON: ${e.message}`)); }
      });
    child.stdin.end(JSON.stringify({ pairs }));
  });
}

export async function computeParamConnectivity({ force = false } = {}) {
  if (!force && cache.data && Date.now() - cache.at < CACHE_TTL_MS) return cache.data;
  const pg = getPool();
  if (!pg) throw new Error('Postgres is required for param connectivity');

  const { rows } = await pg.query(
    `SELECT scanner, param, value FROM scanner_params WHERE variant='champion' ORDER BY scanner, param`);
  const res = await runReader(rows.map(r => ({ scanner: r.scanner, param: r.param, value: r.value })));

  const summary = { CONNECTED: 0, NO_RUNTIME_READ: 0, INDETERMINATE: 0, UNPROVEN: 0, NOT_WIRED: 0 };
  const byScanner = {};
  for (const p of res.pairs) {
    summary[p.status] = (summary[p.status] || 0) + 1;
    const s = (byScanner[p.scanner] ||= { CONNECTED: 0, NO_RUNTIME_READ: 0, INDETERMINATE: 0, UNPROVEN: 0, NOT_WIRED: 0, total: 0 });
    s.execution_state = p.execution_state ?? null;
    s.execution_note = p.execution_note ?? null;
    s[p.status]++; s.total++;
  }

  const data = {
    as_of: new Date().toISOString(),
    scan_window: res.scan_window,
    meaningful_sample: MEANINGFUL_SAMPLE,
    total_rows: res.pairs.length,
    summary,
    by_scanner: byScanner,
    // The one number that says how much of the control panel is real.
    real_control_rows: summary.CONNECTED,
    pairs: res.pairs
  };
  cache = { data, at: Date.now() };
  return data;
}

export function connectivityCacheAge() {
  return cache.data ? Math.round((Date.now() - cache.at) / 1000) : null;
}
