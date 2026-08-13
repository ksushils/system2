// ════════════════════════════════════════════════════════════
// ACCOUNT SIZE — correcting it must not look like a loss.
//
// account_size is the base of the equity curve: equity = account_size +
// realised pnl, and drawdown is measured against a stored high-water
// mark. Lowering account_size to match the broker therefore drops equity
// without changing a single trade, and the HWM -- a historical maximum
// -- does not move with it. Measured today: 60,000 -> 53,329 turns a
// 1.47% drawdown into ~12.6%, crossing BOTH throttle thresholds in one
// step (risk_mult 1.0 -> 0.5) and landing in the band where the
// auto-protective monitor sets paper_only.
//
// So the correction is atomic with an HWM reset, or it is refused. A
// bookkeeping fix must never be indistinguishable from a 12% loss.
//
// The reset caps EVERY equity_curve row, not just today's:
// runEquityCurve recomputes its baseline from
// `SELECT max(high_water_mark) FROM equity_curve`, so capping one row
// would be silently undone on the next run.
// ════════════════════════════════════════════════════════════
import pg from 'pg';

let pool = null;
function getPool() {
  if (pool) return pool;
  if (!process.env.DATABASE_URL) return null;
  pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 2 });
  return pool;
}

// Threshold above which an unreset correction is refused.
export const MAX_UNRESET_DRAWDOWN_MOVE_PP = 2;

// Mirrors runEquityCurve's filters exactly. If these drift apart the
// projection stops describing the thing it is protecting.
const isTestTrade = t =>
  String(t?.scanner || '').toLowerCase() === 'test_harness' ||
  String(t?.ticker || '').toUpperCase().startsWith('ZZ');

export function realisedPnl(db) {
  return (db.data.trades || [])
    .filter(t => String(t.status || '').toUpperCase() === 'CLOSED')
    .filter(t => t.excluded_from_expectancy !== true && t.data?.excluded_from_expectancy !== true)
    .filter(t => !isTestTrade(t))
    .reduce((s, t) => s + (Number(t.pnl_net ?? t.pnl) || 0), 0);
}

export function drawdownPct(equity, hwm) {
  if (!hwm) return 0;
  return Math.max(0, ((hwm - equity) / hwm) * 100);
}

export const riskMultFor = dd => (dd > 10 ? 0.5 : dd >= 5 ? 0.75 : 1.0);

export async function currentState(db) {
  const pgp = getPool();
  if (!pgp) throw new Error('Postgres is required');
  const { rows } = await pgp.query(
    'SELECT max(high_water_mark)::float8 hwm FROM equity_curve');
  const hwm = Number(rows[0]?.hwm) || 0;
  const accountSize = Number(db.data.scanner_config?.account_size) || 0;
  const pnl = realisedPnl(db);
  const equity = accountSize + pnl;
  const dd = drawdownPct(equity, hwm);
  return { accountSize, pnl, equity, hwm, drawdown_pct: +dd.toFixed(2), risk_mult: riskMultFor(dd) };
}

export function project(state, newValue) {
  const equity = newValue + state.pnl;
  const dd = drawdownPct(equity, state.hwm);
  return {
    account_size: newValue, equity, high_water_mark: state.hwm,
    drawdown_pct: +dd.toFixed(2), risk_mult: riskMultFor(dd),
    drawdown_move_pp: +(dd - state.drawdown_pct).toFixed(2)
  };
}

export function attachAccountSize(app, { adminOnly, db, save, sendTelegramAlert, logChangelogRow }) {

  app.get('/api/admin/account-size', adminOnly, async (req, res) => {
    try {
      const state = await currentState(db);
      const preview = req.query.value ? project(state, Number(req.query.value)) : null;
      res.json({ current: state, preview_without_reset: preview,
                 refuse_above_pp: MAX_UNRESET_DRAWDOWN_MOVE_PP });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });

  app.post('/api/admin/account-size', adminOnly, async (req, res) => {
    const pgp = getPool();
    try {
      const { value, reason, reset_hwm = false } = req.body || {};
      const newValue = Number(value);
      if (!Number.isFinite(newValue) || newValue <= 0) {
        return res.status(400).json({ error: 'value must be a positive finite number' });
      }
      if (!String(reason || '').trim()) {
        return res.status(400).json({ error: 'reason required' });
      }
      if (!pgp) return res.status(503).json({ error: 'Postgres unavailable' });

      const before = await currentState(db);
      const projected = project(before, newValue);

      // A correction that would move drawdown materially, without
      // resetting the peak it is measured against, is refused. The
      // projection travels in the error so the caller sees the arithmetic
      // rather than a bare rejection.
      if (!reset_hwm && Math.abs(projected.drawdown_move_pp) > MAX_UNRESET_DRAWDOWN_MOVE_PP) {
        return res.status(409).json({
          status: 'error', reason: 'DRAWDOWN_WOULD_SPIKE',
          message: `changing account_size ${before.accountSize} -> ${newValue} without reset_hwm would move drawdown by ${projected.drawdown_move_pp}pp (${before.drawdown_pct}% -> ${projected.drawdown_pct}%) and risk_mult ${before.risk_mult} -> ${projected.risk_mult}. A bookkeeping correction must not be recorded as a loss. Resend with reset_hwm:true.`,
          before, projected, refuse_above_pp: MAX_UNRESET_DRAWDOWN_MOVE_PP
        });
      }

      const newEquity = newValue + before.pnl;
      const client = await pgp.connect();
      let cappedRows = 0;
      try {
        await client.query('BEGIN');
        if (reset_hwm) {
          // EVERY row, not just today's: runEquityCurve rebuilds its
          // baseline from max(high_water_mark) across the whole table.
          const capped = await client.query(
            'UPDATE equity_curve SET high_water_mark = $1 WHERE high_water_mark > $1', [newEquity]);
          cappedRows = capped.rowCount;
          await client.query(
            `INSERT INTO equity_curve(date,equity,high_water_mark) VALUES(current_date,$1,$1)
             ON CONFLICT(date) DO UPDATE SET equity=$1, high_water_mark=$1`, [newEquity]);
        }
        await client.query('COMMIT');
      } catch (e) {
        await client.query('ROLLBACK');
        throw e;
      } finally { client.release(); }

      // Memory is authoritative for save(), so the config change lands
      // there and is flushed, not written to Postgres alone.
      db.data.scanner_config = db.data.scanner_config || {};
      db.data.scanner_config.account_size = newValue;
      db.data.scanner_config.updated_at = new Date().toISOString();
      await save();

      await logChangelogRow?.({
        scanner: 'all', parameter: 'account_size',
        old_value: String(before.accountSize), new_value: String(newValue),
        reason: String(reason).trim(), approved_by: 'admin-api'
      }).catch(() => {});
      if (reset_hwm) {
        await logChangelogRow?.({
          scanner: 'all', parameter: 'high_water_mark',
          old_value: String(before.hwm), new_value: String(newEquity),
          reason: `HWM reset with account_size change: ${String(reason).trim()}`,
          approved_by: 'admin-api'
        }).catch(() => {});
      }

      const after = await currentState(db);
      await sendTelegramAlert?.(
        `ACCOUNT SIZE CHANGED\n${before.accountSize} -> ${newValue}\n` +
        `hwm ${before.hwm} -> ${after.hwm}${reset_hwm ? ' (reset)' : ' (unchanged)'}\n` +
        `drawdown ${before.drawdown_pct}% -> ${after.drawdown_pct}%  risk_mult ${before.risk_mult} -> ${after.risk_mult}\n` +
        `reason: ${String(reason).trim()}`
      ).catch(() => {});

      res.json({ status: 'ok', before, after, reset_hwm: !!reset_hwm,
                 equity_curve_rows_capped: cappedRows });
    } catch (e) { res.status(500).json({ error: e.message }); }
  });
}
