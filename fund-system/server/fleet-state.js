// ════════════════════════════════════════════════════════════
// FLEET DRIFT
//
// Compares what we EXPECT to be running against what n8n is ACTUALLY
// running. Actual state is read from n8n itself (read-only), never
// inferred from our own records — inferring is how volume's and comm's
// v7 rows sat dormant from 2026-08-07 while broken v6 versions ran.
//
// Three distinctions this file exists to preserve:
//
//  1. A heartbeat is NOT a trading-path execution. comm's heartbeat node
//     authenticates with n8n credential kSRmNF022fIz9U1r, which was
//     always valid, so its heartbeat landed while its Code nodes were
//     401ing. crypto heartbeats every 5 minutes and has never once
//     reached its trading path. Heartbeat liveness says nothing about
//     whether a scanner can trade.
//  2. "Never executed" is NOT "failing". pa/fmp/fmp_alpaca had zero
//     heartbeats purely because no weekday had occurred since deploy.
//     Execution count separates the two.
//  3. A dormant NEWER version beside the active one is invisible unless
//     you look for it explicitly.
//
// computeFleetState is pure so the drift logic can be tested without
// touching n8n or the clock.
// ════════════════════════════════════════════════════════════
import { execFile } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

export const UNKNOWN = 'UNKNOWN';

// Schedule classes. Weekday-only scanners idle at the weekend are not
// drifting, they are simply out of session.
export const FLEET_SCHEDULES = {
  crypto: '24/7',
  indices: 'weekday-only',
  comm: 'weekday-only',
  volume: 'weekday-only',
  mean_reversion: 'weekday-only',
  forex: 'weekday-only',
  pa: 'weekday-only',
  fmp: 'weekday-only',
  fmp_alpaca: 'weekday-only',
  failed_breakout: UNKNOWN
};

export function readActualFleet({ timeoutMs = 25000, db = null } = {}) {
  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    if (db) env.N8N_SQLITE_DB = db;
    execFile('python3', [path.join(HERE, 'fleet-actual.py')],
      { env, timeout: timeoutMs, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) return reject(new Error(`fleet-actual failed: ${err.message} ${String(stderr).slice(0, 200)}`));
        try { resolve(JSON.parse(stdout)); }
        catch (e) { reject(new Error(`fleet-actual returned non-JSON: ${e.message}`)); }
      });
  });
}

// Saturday or Sunday in ET, which is what the trading calendar follows.
export function isWeekendET(now = new Date()) {
  const wd = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short' })
    .format(now);
  return wd === 'Sat' || wd === 'Sun';
}

const ageSeconds = (iso, now) => {
  if (!iso) return null;
  const t = Date.parse(String(iso).includes('T') ? iso : String(iso).replace(' ', 'T') + 'Z');
  return Number.isFinite(t) ? Math.round((now.getTime() - t) / 1000) : null;
};

/**
 * @param expected  rows from fleet_expected, keyed by scanner
 * @param actual    output of fleet-actual.py
 * @param heartbeats {scanner: epochMs}
 */
export function computeFleetState({ expected = {}, actual = {}, heartbeats = {}, now = new Date() }) {
  const weekend = isWeekendET(now);
  const scanners = [];
  const alerts = [];

  const names = new Set([...Object.keys(FLEET_SCHEDULES), ...Object.keys(expected), ...Object.keys(actual.scanners || {})]);

  for (const s of [...names].sort()) {
    const exp = expected[s] || {};
    const act = (actual.scanners || {})[s] || {};
    const schedule = exp.schedule || FLEET_SCHEDULES[s] || UNKNOWN;

    const shouldBeActive = exp.should_be_active === undefined || exp.should_be_active === null
      ? null : !!exp.should_be_active;
    const actualActiveCount = Number(act.actual_active_count || 0);
    const actualActive = actualActiveCount > 0;

    const expVersion = exp.expected_version_id || null;
    const actVersion = act.actual_active_version_id || null;

    // Drift is a mismatch against what we EXPECT. Staleness is reported
    // separately so a quiet weekend never reads as drift.
    const activeMismatch = shouldBeActive === null ? false
      : (shouldBeActive ? actualActiveCount !== 1 : actualActiveCount !== 0);
    const versionMismatch = !!(expVersion && actVersion && expVersion !== actVersion);
    const versionUnverifiable = !expVersion;

    const hbMs = heartbeats[s];
    const hbAge = Number.isFinite(hbMs) ? Math.round((now.getTime() - hbMs) / 1000) : null;

    const execCount = Number(act.execution_count || 0);
    const neverExecuted = execCount === 0;

    const row = {
      scanner: s,
      schedule,
      should_be_active: shouldBeActive === null ? UNKNOWN : shouldBeActive,
      actual_active: actualActive,
      actual_active_count: actualActiveCount,
      actual_workflow_id: act.actual_workflow_id || null,
      expected_version_id: expVersion || UNKNOWN,
      actual_active_version_id: actVersion || UNKNOWN,
      expected_hash: exp.expected_hash || UNKNOWN,
      versions_total: Number(act.versions_total || 0),

      // liveness, kept strictly apart
      last_heartbeat_age_s: hbAge,
      execution_count: execCount,
      execution_count_7d: Number(act.execution_count_7d || 0),
      last_execution_at: act.last_execution_at || null,
      last_trading_path_at: act.last_trading_path_at || null,
      last_trading_path_age_s: ageSeconds(act.last_trading_path_at, now),
      trading_path_scan_window: act.trading_path_scan_window ?? null,
      never_executed: neverExecuted,
      // a heartbeat proves reachability, not tradability
      heartbeat_proves_trading: false,
      liveness: neverExecuted
        ? 'NEVER_EXECUTED'
        : (act.last_trading_path_at ? 'TRADING_PATH_SEEN' : 'EXECUTING_NO_TRADING_PATH'),

      dormant_newer_version: act.dormant_newer
        ? { id: act.dormant_newer.id, imported_at: act.dormant_newer.imported_at, name: act.dormant_newer.name }
        : null,

      version_unverifiable: versionUnverifiable,
      drift: activeMismatch || versionMismatch,
      drift_reasons: []
    };

    if (activeMismatch) {
      row.drift_reasons.push(
        `active mismatch: expected ${shouldBeActive ? 'exactly 1 active' : '0 active'}, actual ${actualActiveCount}`);
    }
    if (versionMismatch) {
      row.drift_reasons.push(`version mismatch: expected ${expVersion}, actual ${actVersion}`);
    }
    if (row.drift) {
      alerts.push({ scanner: s, expected: shouldBeActive ? (expVersion || 'active') : 'inactive',
                    actual: actualActive ? (actVersion || `${actualActiveCount} active`) : 'inactive',
                    reasons: row.drift_reasons.slice() });
    }

    // Weekend-aware staleness: informational, never drift.
    row.stale = false;
    if (shouldBeActive === true && !neverExecuted) {
      const weekdayOnly = schedule === 'weekday-only';
      if (!(weekdayOnly && weekend)) {
        const age = ageSeconds(act.last_execution_at, now);
        row.stale = age === null ? true : age > 24 * 3600;
      }
    }
    scanners.push(row);
  }

  const ghosts = (actual.ghosts || []).map(g => ({ ...g }));
  for (const g of ghosts) {
    alerts.push({ scanner: `(ghost) ${g.name}`, expected: 'inactive with no activeVersionId',
                  actual: `inactive but holding activeVersionId ${g.activeVersionId}`,
                  reasons: ['ghost: inactive workflow still holds a live activeVersionId'] });
  }

  const dormant = scanners.filter(r => r.dormant_newer_version);
  // A scanner we deliberately keep inactive has no expected version to
  // record, so it is not an unknown -- flagging it would be permanent
  // noise in the health warnings.
  const unknownExpected = scanners.filter(r =>
    r.should_be_active === UNKNOWN ||
    (r.should_be_active === true && r.expected_version_id === UNKNOWN));

  return {
    generated_at: now.toISOString(),
    weekend_et: weekend,
    active_total: Number(actual.active_total || 0),
    scanners,
    ghosts,
    alerts,
    summary: {
      drift_count: scanners.filter(r => r.drift).length,
      ghost_count: ghosts.length,
      dormant_newer_count: dormant.length,
      unknown_expected: unknownExpected.map(r => r.scanner),
      stale_count: scanners.filter(r => r.stale).length
    }
  };
}

export function fleetWarnings(state) {
  const w = [];
  if (state.summary.drift_count) w.push(`fleet_drift:${state.summary.drift_count}`);
  if (state.summary.ghost_count) w.push(`fleet_ghost_workflows:${state.summary.ghost_count}`);
  if (state.summary.dormant_newer_count) w.push(`fleet_dormant_newer_version:${state.summary.dormant_newer_count}`);
  if (state.summary.unknown_expected.length) w.push(`fleet_expected_unknown:${state.summary.unknown_expected.join(',')}`);
  return w;
}
