import pg from 'pg';

const DATABASE_URL = process.env.DATABASE_URL || '';
const pool = DATABASE_URL ? new pg.Pool({ connectionString: DATABASE_URL }) : null;
const WINDOW_HOURS = 24;
const STALE_HOURS = 30;
let initialized = false;
let lastRunEtDay = '';
let lastStaleAlertAt = 0;

function etParts(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false
  }).formatToParts(date);
  return Object.fromEntries(parts.map(p => [p.type, p.value]));
}

function etDateKey(date = new Date()) {
  const p = etParts(date);
  return `${p.year}-${p.month}-${p.day}`;
}

function addDays(dateKey, days) {
  const d = new Date(`${dateKey}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function easternEventDate(dateKey, timing) {
  const hour = String(timing || '').toLowerCase() === 'bmo' ? 8 : String(timing || '').toLowerCase() === 'amc' ? 16 : 12;
  const [year, month, day] = dateKey.split('-').map(Number);
  const guess = Date.UTC(year, month - 1, day, hour);
  const observed = etParts(new Date(guess));
  const observedAsUtc = Date.UTC(Number(observed.year), Number(observed.month) - 1, Number(observed.day), Number(observed.hour), Number(observed.minute));
  return new Date(guess + (guess - observedAsUtc));
}

export async function initEarningsGuard() {
  if (!pool || initialized) return !!pool;
  await pool.query(`
    CREATE TABLE IF NOT EXISTS earnings_calendar_cache (
      symbol text NOT NULL,
      earnings_date date NOT NULL,
      timing text,
      event_at timestamptz NOT NULL,
      fetched_at timestamptz NOT NULL DEFAULT now(),
      raw jsonb NOT NULL DEFAULT '{}'::jsonb,
      PRIMARY KEY(symbol, earnings_date)
    );
    CREATE TABLE IF NOT EXISTS earnings_calendar_sync (
      singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
      fetched_at timestamptz,
      status text NOT NULL DEFAULT 'never',
      row_count integer NOT NULL DEFAULT 0,
      error text
    );
    INSERT INTO earnings_calendar_sync(singleton) VALUES(true) ON CONFLICT(singleton) DO NOTHING;
  `);
  initialized = true;
  return true;
}

export async function refreshEarningsCalendar(fmpProxyFetch) {
  await initEarningsGuard();
  if (!pool) throw new Error('Postgres unavailable');
  const from = etDateKey();
  const to = addDays(from, 3);
  try {
    const result = await fmpProxyFetch(`earnings-calendar:${from}:${to}`, 20 * 60_000,
      `stable/earnings-calendar?from=${from}&to=${to}`);
    if (!result?.ok || !Array.isArray(result.body)) throw new Error(`FMP earnings calendar status ${result?.status || 'unknown'}`);
    const rows = result.body.filter(row => row?.symbol && row?.date);
    await pool.query('BEGIN');
    try {
      await pool.query('DELETE FROM earnings_calendar_cache WHERE earnings_date BETWEEN $1 AND $2', [from, to]);
      await pool.query("DELETE FROM earnings_calendar_cache WHERE earnings_date < current_date - interval '7 days'");
      for (const row of rows) {
        const symbol = String(row.symbol).trim().toUpperCase();
        const date = String(row.date).slice(0, 10);
        const timing = String(row.time || row.timing || '').toLowerCase() || null;
        const eventAt = easternEventDate(date, timing);
        await pool.query(`INSERT INTO earnings_calendar_cache(symbol,earnings_date,timing,event_at,fetched_at,raw)
          VALUES($1,$2,$3,$4,now(),$5) ON CONFLICT(symbol,earnings_date) DO UPDATE
          SET timing=excluded.timing,event_at=excluded.event_at,fetched_at=excluded.fetched_at,raw=excluded.raw`,
          [symbol, date, timing, eventAt, row]);
      }
      await pool.query(`UPDATE earnings_calendar_sync SET fetched_at=now(),status='ok',row_count=$1,error=NULL WHERE singleton`, [rows.length]);
      await pool.query('COMMIT');
    } catch (e) { await pool.query('ROLLBACK'); throw e; }
    return { ok: true, from, to, rows: rows.length, cache: result.cache };
  } catch (e) {
    await pool.query(`UPDATE earnings_calendar_sync SET status='error',error=$1 WHERE singleton`, [e.message]).catch(() => {});
    throw e;
  }
}

export async function guardResult(ticker, now = new Date(), simulateStale = false) {
  await initEarningsGuard();
  if (!pool) return { blocked: false, earnings_date: null, hours_until: null, window_hours: WINDOW_HOURS, stale: true, error: 'cache unavailable' };
  const { rows: syncRows } = await pool.query('SELECT * FROM earnings_calendar_sync WHERE singleton');
  const sync = syncRows[0] || {};
  const ageHours = sync.fetched_at ? (now - new Date(sync.fetched_at)) / 3_600_000 : Infinity;
  const stale = simulateStale || !Number.isFinite(ageHours) || ageHours > STALE_HOURS;
  if (stale) return { blocked: false, earnings_date: null, hours_until: null, window_hours: WINDOW_HOURS, stale: true, cache_age_hours: Number.isFinite(ageHours) ? +ageHours.toFixed(2) : null };
  const { rows } = await pool.query(`SELECT symbol,earnings_date::text AS earnings_date,event_at,timing FROM earnings_calendar_cache
    WHERE symbol=$1 AND event_at >= $2 ORDER BY event_at LIMIT 1`, [ticker, new Date(now.getTime() - WINDOW_HOURS * 3_600_000)]);
  const event = rows[0];
  if (!event) return { blocked: false, earnings_date: null, hours_until: null, window_hours: WINDOW_HOURS, stale: false };
  const hoursUntil = (new Date(event.event_at) - now) / 3_600_000;
  return { blocked: hoursUntil >= 0 && hoursUntil <= WINDOW_HOURS, earnings_date: String(event.earnings_date).slice(0, 10), hours_until: +hoursUntil.toFixed(2), window_hours: WINDOW_HOURS, stale: false };
}

async function warnStale(sendTelegramAlert, ticker, result, force = false) {
  if (!result.stale || !sendTelegramAlert) return null;
  if (!force && Date.now() - lastStaleAlertAt < 6 * 3_600_000) return { ok: true, skipped: 'stale warning cooldown' };
  const telegram = await sendTelegramAlert(`EARNINGS GUARD STALE: cache is older than ${STALE_HOURS}h; fail-open is active and ${ticker} trading continues. Check FMP/proxy availability.`);
  if (telegram.ok) lastStaleAlertAt = Date.now();
  return telegram;
}

export function attachEarningsGuard(app, { scannerAuth, adminOnly, fmpProxyFetch, sendTelegramAlert }) {
  app.get('/api/guard/earnings', scannerAuth, async (req, res) => {
    try {
      const ticker = String(req.query.ticker || '').trim().toUpperCase();
      if (!/^[A-Z0-9.^-]{1,20}$/.test(ticker)) return res.status(400).json({ error: 'valid ticker required' });
      const result = await guardResult(ticker);
      if (result.stale) result.warning = await warnStale(sendTelegramAlert, ticker, result);
      res.json(result);
    } catch (e) { res.status(500).json({ error: e.message }); }
  });
  app.post('/api/admin/guard/earnings/refresh', adminOnly, async (_req, res) => {
    try { res.json(await refreshEarningsCalendar(fmpProxyFetch)); } catch (e) { res.status(502).json({ error: e.message }); }
  });
  app.post('/api/admin/guard/earnings/stale-test', adminOnly, async (req, res) => {
    try {
      const ticker = String(req.body?.ticker || 'TEST').toUpperCase();
      const result = await guardResult(ticker, new Date(), true);
      result.warning = await warnStale(sendTelegramAlert, ticker, result, true);
      res.json(result);
    } catch (e) { res.status(500).json({ error: e.message }); }
  });
  app.post('/api/admin/guard/earnings/window-test', adminOnly, async (req, res) => {
    const hoursUntil = Number(req.body?.hours_until);
    if (!Number.isFinite(hoursUntil)) return res.status(400).json({ error: 'hours_until required' });
    res.json({
      ticker: String(req.body?.ticker || 'TEST').toUpperCase(),
      blocked: hoursUntil >= 0 && hoursUntil <= WINDOW_HOURS,
      earnings_date: new Date(Date.now() + hoursUntil * 3_600_000).toISOString().slice(0, 10),
      hours_until: hoursUntil,
      window_hours: WINDOW_HOURS,
      stale: false,
      simulation: true
    });
  });
}

export function startEarningsGuardJobs({ fmpProxyFetch } = {}) {
  setInterval(() => {
    const p = etParts();
    const day = `${p.year}-${p.month}-${p.day}`;
    if (p.hour !== '06' || lastRunEtDay === day) return;
    lastRunEtDay = day;
    refreshEarningsCalendar(fmpProxyFetch).catch(e => console.error('[earnings-calendar]', e.message));
  }, 60_000).unref?.();
}
