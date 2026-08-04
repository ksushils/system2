import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const SCANNER_ORDER = [
  'fmp',
  'fmp_alpaca',
  'forex',
  'comm',
  'pa',
  'failed_breakout',
  'volume',
  'mean_reversion',
  'indices',
  'crypto'
];

const ALIASES = {
  fb: 'failed_breakout',
  failedbo: 'failed_breakout',
  failed_breakout: 'failed_breakout',
  vp: 'volume',
  vol: 'volume',
  volume_profile: 'volume',
  volume: 'volume',
  mr: 'mean_reversion',
  meanreversion: 'mean_reversion',
  mean_reversion: 'mean_reversion',
  commodity: 'comm',
  comm: 'comm',
  fmp: 'fmp',
  fmp_alpaca: 'fmp_alpaca',
  forex: 'forex',
  pa: 'pa',
  indices: 'indices',
  crypto: 'crypto'
};

let pool = null;
let ready = false;

function normalizeScanner(scanner) {
  const key = String(scanner || '').trim().toLowerCase();
  return ALIASES[key] || key;
}

async function getPool() {
  if (pool) return pool;
  if (!process.env.DATABASE_URL) return null;
  const pg = (await import('pg')).default;
  pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 5 });
  return pool;
}

function seedRows() {
  const file = path.join(__dirname, 'scanner-param-seeds.json');
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

export function castParamValue(value, valueType) {
  if (valueType === 'number') return Number(value);
  if (valueType === 'bool') return String(value).toLowerCase() === 'true';
  return String(value);
}

async function ensureReady() {
  const pg = await getPool();
  if (!pg) return false;
  if (ready) return true;
  await pg.query(`
    CREATE TABLE IF NOT EXISTS scanner_params (
      id serial PRIMARY KEY,
      scanner text NOT NULL,
      param text NOT NULL,
      value text NOT NULL,
      value_type text NOT NULL CHECK (value_type IN ('number','bool','string')),
      variant text NOT NULL DEFAULT 'champion',
      updated_at timestamptz NOT NULL DEFAULT now(),
      updated_by text,
      UNIQUE(scanner, param, variant)
    )
  `);
  await pg.query(`
    CREATE TABLE IF NOT EXISTS intelligence_changelog (
      id serial PRIMARY KEY,
      date timestamptz NOT NULL DEFAULT now(),
      scanner text,
      parameter text,
      old_value text,
      new_value text,
      reason text,
      approved_by text
    )
  `);
  for (const r of seedRows()) {
    await pg.query(`
      INSERT INTO scanner_params(scanner,param,value,value_type,variant,updated_by)
      VALUES($1,$2,$3,$4,'champion','seed-v6-config')
      ON CONFLICT(scanner,param,variant) DO NOTHING
    `, [normalizeScanner(r.scanner), r.param, String(r.value), r.value_type]);
  }
  ready = true;
  return true;
}

export async function initParamStore() {
  return ensureReady();
}

export async function getScannerParams(scanner, variant = 'champion') {
  const ok = await ensureReady();
  if (!ok) return { params: {}, params_updated_at: null, rows: [] };
  const pg = await getPool();
  const s = normalizeScanner(scanner);
  const result = await pg.query(`
    SELECT scanner,param,value,value_type,variant,updated_at,updated_by
    FROM scanner_params
    WHERE scanner=$1 AND variant=$2
    ORDER BY param
  `, [s, variant]);
  const params = {};
  let latest = null;
  for (const row of result.rows) {
    params[row.param] = castParamValue(row.value, row.value_type);
    if (!latest || new Date(row.updated_at) > new Date(latest)) latest = row.updated_at;
  }
  return { params, params_updated_at: latest, rows: result.rows };
}

function validateParamValue(row, rawValue) {
  const value = String(rawValue);
  if (!value.length) return { ok: false, error: 'value required' };
  if (row.value_type === 'bool') {
    if (!/^(true|false)$/i.test(value)) return { ok: false, error: 'boolean value must be true or false' };
    return { ok: true, value: value.toLowerCase() };
  }
  if (row.value_type === 'number') {
    const n = Number(value);
    if (!Number.isFinite(n)) return { ok: false, error: 'numeric value must parse as a finite number' };
    const p = row.param.toUpperCase();
    const allowNegative = /PULLBACK|CRASH|LOSS_LIMIT/.test(p) && !/RISK|HEAT/.test(p);
    if (!allowNegative && n < 0) return { ok: false, error: 'negative values are not allowed for this parameter' };
    const mustBePositive = /RISK|HEAT|LOOKBACK|LIMIT|WINDOW|BARS|MINUTES|HOURS|MAX_|MIN_|SCORE|SIZE|PRICE|VOLUME|VOL|RRR|RATIO|ATR|PIPS|LOTS|POSITIONS|TRADES|COOLDOWN|LEVERAGE|MARGIN|TP|SL|STOP/.test(p);
    if (mustBePositive && n <= 0) return { ok: false, error: 'zero or negative values would silently break this parameter' };
    return { ok: true, value: String(n) };
  }
  return { ok: true, value };
}

function eventTime(row) {
  const ts = row.created_at || row.closed_at || row.opened_at || row.ts || row.timestamp || row.data?.ts || row.data?.timestamp;
  const d = ts ? new Date(ts) : null;
  return d && Number.isFinite(d.getTime()) ? d : null;
}

function ageMinutes(date) {
  if (!date) return null;
  return Math.round((Date.now() - date.getTime()) / 60000);
}

function scannerOf(row) {
  return normalizeScanner(row.scanner || row.data?.scanner);
}

function topReason(rejections, scanner) {
  const weekAgo = Date.now() - 7 * 86400000;
  const counts = new Map();
  for (const r of rejections || []) {
    if (scannerOf(r) !== scanner) continue;
    const d = eventTime(r);
    if (!d || d.getTime() < weekAgo) continue;
    const reason = r.reason || r.data?.reason || 'UNKNOWN';
    counts.set(reason, (counts.get(reason) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0] || null;
}

function expectancy(trades, scanner) {
  const closed = (trades || [])
    .filter(t => scannerOf(t) === scanner && String(t.status || t.data?.status || '').toUpperCase() === 'CLOSED')
    .sort((a, b) => (eventTime(b)?.getTime() || 0) - (eventTime(a)?.getTime() || 0))
    .slice(0, 30);
  if (closed.length < 30) return { status: 'insufficient', count: closed.length };
  const pnl = closed.map(t => Number(t.pnl ?? t.data?.pnl ?? 0));
  const avg = pnl.reduce((s, n) => s + n, 0) / pnl.length;
  const wins = pnl.filter(n => n > 0).length;
  return { status: 'ok', count: closed.length, expectancy: +avg.toFixed(2), win_rate_pct: +(wins / pnl.length * 100).toFixed(1) };
}

async function scorecardRows(db) {
  await ensureReady();
  const pg = await getPool();
  const paramRows = pg ? (await pg.query(`
    SELECT p.*, c.date AS last_change_date, c.old_value, c.new_value, c.reason AS change_reason
    FROM scanner_params p
    LEFT JOIN LATERAL (
      SELECT date, old_value, new_value, reason
      FROM intelligence_changelog c
      WHERE c.scanner=p.scanner AND c.parameter=p.param
      ORDER BY date DESC LIMIT 1
    ) c ON true
    WHERE p.variant='champion'
    ORDER BY p.scanner,p.param
  `)).rows : [];
  const groupedParams = {};
  for (const p of paramRows) {
    groupedParams[p.scanner] = groupedParams[p.scanner] || [];
    groupedParams[p.scanner].push({
      param: p.param,
      value: castParamValue(p.value, p.value_type),
      raw_value: p.value,
      value_type: p.value_type,
      updated_at: p.updated_at,
      last_change_date: p.last_change_date,
      last_change: p.last_change_date ? {
        date: p.last_change_date,
        old_value: p.old_value,
        new_value: p.new_value,
        reason: p.change_reason
      } : null
    });
  }
  const hb = db.data.heartbeats || {};
  return SCANNER_ORDER.map(scanner => {
    const heartbeat = hb[scanner] || null;
    const signalDates = (db.data.signals || []).filter(r => scannerOf(r) === scanner).map(eventTime).filter(Boolean);
    const tradeDates = (db.data.trades || []).filter(r => scannerOf(r) === scanner).map(eventTime).filter(Boolean);
    const rejectionDates = (db.data.rejections || []).filter(r => scannerOf(r) === scanner).map(eventTime).filter(Boolean);
    const reason = topReason(db.data.rejections || [], scanner);
    const params = groupedParams[scanner] || [];
    const lastChange = params.map(p => p.last_change).filter(Boolean).sort((a, b) => new Date(b.date) - new Date(a.date))[0] || null;
    return {
      scanner,
      heartbeat: heartbeat ? {
        ...heartbeat,
        age_minutes: ageMinutes(new Date(Number(heartbeat.ts) || heartbeat.iso || heartbeat.ts)),
        state: ageMinutes(new Date(Number(heartbeat.ts) || heartbeat.iso || heartbeat.ts)) <= 30 ? 'ALIVE' : 'STALE'
      } : { state: 'MISSING', age_minutes: null },
      productivity: {
        last_signal_age_minutes: ageMinutes(signalDates.sort((a, b) => b - a)[0]),
        last_trade_age_minutes: ageMinutes(tradeDates.sort((a, b) => b - a)[0]),
        last_rejection_age_minutes: ageMinutes(rejectionDates.sort((a, b) => b - a)[0])
      },
      expectancy_30: expectancy(db.data.trades || [], scanner),
      top_rejection_reason_7d: reason ? { reason: reason[0], count: reason[1] } : null,
      params_source: params.length ? 'server' : 'local',
      params,
      last_param_change: lastChange
    };
  });
}

export function attachParamStore(app, db, { adminOnly }) {
  app.post('/api/params', adminOnly, async (req, res) => {
    try {
      const { scanner, param, value, reason } = req.body || {};
      const s = normalizeScanner(scanner);
      const p = String(param || '').trim();
      if (!s || !p) return res.status(400).json({ error: 'scanner and param required' });
      if (!String(reason || '').trim()) return res.status(400).json({ error: 'reason required' });
      const ok = await ensureReady();
      if (!ok) return res.status(503).json({ error: 'parameter store unavailable' });
      const pg = await getPool();
      const found = await pg.query(`SELECT * FROM scanner_params WHERE scanner=$1 AND param=$2 AND variant='champion'`, [s, p]);
      if (!found.rows.length) return res.status(400).json({ error: 'unknown scanner parameter', scanner: s, param: p });
      const row = found.rows[0];
      const validation = validateParamValue(row, value);
      if (!validation.ok) return res.status(400).json({ error: validation.error, scanner: s, param: p });
      await pg.query('BEGIN');
      try {
        await pg.query(`
          UPDATE scanner_params
          SET value=$1, updated_at=now(), updated_by='admin-api'
          WHERE scanner=$2 AND param=$3 AND variant='champion'
        `, [validation.value, s, p]);
        const log = await pg.query(`
          INSERT INTO intelligence_changelog(scanner, parameter, old_value, new_value, reason, approved_by)
          VALUES($1,$2,$3,$4,$5,'admin-api')
          RETURNING *
        `, [s, p, row.value, validation.value, String(reason).trim()]);
        await pg.query('COMMIT');
        res.json({ status: 'ok', scanner: s, param: p, old_value: row.value, new_value: validation.value, changelog: log.rows[0] });
      } catch (e) {
        await pg.query('ROLLBACK');
        throw e;
      }
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/scorecard', adminOnly, async (req, res) => {
    try {
      res.json({ rows: await scorecardRows(db), generated_at: new Date().toISOString() });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });
}

export { SCANNER_ORDER };
