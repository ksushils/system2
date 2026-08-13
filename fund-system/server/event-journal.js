import pg from 'pg';

const { Pool } = pg;
const pool = process.env.DATABASE_URL ? new Pool({ connectionString: process.env.DATABASE_URL, max: 2, application_name: 'fund-event-journal' }) : null;
const KINDS = new Set(['signal','rejection','order_placed','trade_open','trade_close','param_change','halt','breaker_trip','brain_veto','external_close','id_drift','scanner_error','resume','price_tick_rejected']);

export async function initEventJournal() {
  if (!pool) return false;
  await pool.query(`
    CREATE TABLE IF NOT EXISTS events (
      id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), kind TEXT NOT NULL,
      scanner TEXT, ticker TEXT, payload JSONB NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind,ts DESC);
    CREATE OR REPLACE FUNCTION reject_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'events is append-only'; END $$;
    DROP TRIGGER IF EXISTS events_append_only ON events;
    CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
      FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON events FROM PUBLIC;
  `);
  return true;
}

export async function journalEvent(kind, value = {}) {
  if (!pool || !KINDS.has(kind)) return null;
  const payload = value.payload || value;
  const { rows } = await pool.query(
    `INSERT INTO events(kind,scanner,ticker,payload) VALUES($1,$2,$3,$4) RETURNING *`,
    [kind, value.scanner || payload.scanner || null, value.ticker || payload.ticker || null, payload]
  );
  return rows[0];
}

// Used by /api/health (24h count, warning-only) and the daily digest
// (breakdown by scanner+stage). Both need a live query -- scanner_error
// events arrive from a fire-and-forget journalEvent() call, so nothing else
// in memory reflects them.
export async function scannerErrorStats(sinceMs = 24 * 60 * 60_000) {
  if (!pool) return { total: 0, by_scanner_stage: [] };
  const { rows } = await pool.query(
    `SELECT scanner, payload->>'stage' AS stage, payload->>'error_class' AS error_class, count(*)::int n
     FROM events
     WHERE kind='scanner_error' AND ts >= now() - ($1 || ' milliseconds')::interval
     GROUP BY scanner, payload->>'stage', payload->>'error_class'
     ORDER BY n DESC`,
    [sinceMs]
  );
  const total = rows.reduce((s, r) => s + r.n, 0);
  return { total, by_scanner_stage: rows };
}

export function attachEventJournal(app, { adminOnly }) {
  app.get('/api/events', adminOnly, async (req, res) => {
    try {
      if (!pool) return res.status(503).json({ error: 'event journal unavailable' });
      const values = [];
      const where = [];
      if (req.query.since) { values.push(req.query.since); where.push(`ts >= $${values.length}::timestamptz`); }
      if (req.query.kind) { values.push(String(req.query.kind)); where.push(`kind = $${values.length}`); }
      const limit = Math.min(1000, Math.max(1, Number(req.query.limit || 200)));
      values.push(limit);
      const { rows } = await pool.query(`SELECT * FROM events ${where.length ? `WHERE ${where.join(' AND ')}` : ''} ORDER BY ts DESC LIMIT $${values.length}`, values);
      res.json({ events: rows });
    } catch (error) { res.status(400).json({ error: error.message }); }
  });
}
