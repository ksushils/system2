// OOS WINDOW GUARD — run daily.
//
// Recomputes each OPEN window's config hash from the WORKFLOW LITERALS and
// compares it to the hash stored when the window opened. A parameter touched
// mid-window destroys the test, so on divergence the window is marked
// INVALIDATED, the changed keys are reported, and Telegram is alerted.
//
// Reads the snapshot JSON produced by scripts/oos-snapshot.py (python owns the
// n8n sqlite read; better-sqlite3 is not a dependency of this project).
//
// Alerts on the TRANSITION only would repeat deployed_file_guard's weakness —
// this alerts whenever a window is newly INVALIDATED, which is a one-way state
// change, so it fires exactly once per window by construction.
import pg from 'pg';
import fs from 'node:fs';

const SNAP = process.env.OOS_SNAPSHOT || '/tmp/oos_snap.json';
const APPLY = process.argv.includes('--apply');
const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 2, connectionTimeoutMillis: 5000 });

async function alert(text) {
  const token = process.env.TELEGRAM_BOT_TOKEN, chat = process.env.TELEGRAM_CHAT_ID;
  if (!token || !chat) return { sent: false, reason: 'missing Telegram credentials' };
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // HTML, never Markdown: scanner names and param keys carry underscores.
      body: JSON.stringify({ chat_id: chat, text, parse_mode: 'HTML' })
    });
    return { sent: r.ok, status: r.status, body: (await r.text()).slice(0, 160) };
  } catch (e) { return { sent: false, error: String(e.message).slice(0, 120) }; }
}

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

let snap;
try { snap = JSON.parse(fs.readFileSync(SNAP, 'utf8')); }
catch (e) { console.log(JSON.stringify({ ok: false, error: 'snapshot unreadable: ' + e.message })); process.exit(1); }

async function main() {
const open = (await pool.query(
  `SELECT id, scanner, opened_at, config_hash, params_json, in_sample_n, target_n
     FROM oos_windows WHERE status='OPEN' ORDER BY id`)).rows;

const report = { checked_at: new Date().toISOString(), open_windows: open.length, results: [] };

for (const w of open) {
  const cur = snap[w.scanner];
  if (!cur) {
    report.results.push({ id: w.id, scanner: w.scanner, verdict: 'NO_SNAPSHOT' });
    continue;
  }
  // progress: closed trades since the window opened
  const prog = await pool.query(
    `SELECT count(*)::int n FROM trades
      WHERE data->>'scanner'=$1 AND data->>'status'='CLOSED' AND data->>'pnl' IS NOT NULL
        AND (data->>'closed_at')::timestamptz > $2`, [w.scanner, w.opened_at]);
  const accrued = prog.rows[0].n;

  if (cur.config_hash === w.config_hash) {
    report.results.push({ id: w.id, scanner: w.scanner, verdict: 'OK', accrued, target_n: w.target_n });
    continue;
  }

  // what changed, key by key.
  // pg returns a JSONB column as a parsed OBJECT, not a string — JSON.parse on
  // it throws "[object Object] is not valid JSON". Accept either shape.
  const before = typeof w.params_json === 'string'
    ? JSON.parse(w.params_json || '{}')
    : (w.params_json || {});
  const after = cur.params;
  const changed = [];
  for (const k of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (String(before[k]) !== String(after[k]))
      changed.push({ key: k, was: before[k] ?? '(absent)', now: after[k] ?? '(absent)' });
  }
  report.results.push({
    id: w.id, scanner: w.scanner, verdict: 'INVALIDATED', accrued, target_n: w.target_n,
    opened_at: w.opened_at, stored_hash: w.config_hash, current_hash: cur.config_hash, changed
  });

  if (APPLY) {
    await pool.query(
      `UPDATE oos_windows SET status='INVALIDATED', closed_at=now(),
              verdict=$2 WHERE id=$1 AND status='OPEN'`,
      [w.id, `config changed mid-window: ${changed.map(c => c.key).join(',')}`]);
    const lines = changed.slice(0, 12).map(c => `• <b>${esc(c.key)}</b>: ${esc(c.was)} → ${esc(c.now)}`).join('\n');
    report.results[report.results.length - 1].telegram = await alert(
      `<b>OOS WINDOW INVALIDATED</b>\nscanner: ${esc(w.scanner)}\nopened: ${esc(String(w.opened_at).slice(0, 19))}\n` +
      `progress: ${accrued}/${w.target_n} trades\nhash ${esc(w.config_hash)} → ${esc(cur.config_hash)}\n${lines}\n` +
      `A parameter changed mid-window. The out-of-sample test for this config is void.`);
  }
}

console.log(JSON.stringify({ ok: true, ...report }, null, 2));
}

try { await main(); }
catch (error) {
  console.log(JSON.stringify({ ok: false, state: 'DATABASE_OR_GUARD_FAILURE', code: error?.code || null, error: String(error?.message || error).slice(0, 240) }));
  process.exitCode = 2;
} finally { await pool.end().catch(() => {}); }
