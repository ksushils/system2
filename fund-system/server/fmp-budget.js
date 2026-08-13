// ════════════════════════════════════════════════════════════
// FMP PROXY BUDGET STORE
//
// Pure file-backed accounting for the FMP proxy. No side effects on
// import, every path passed in — so this can be unit-tested against a
// temp directory without starting the server.
//
// Why it exists: the old counter kept a single day and no history, so
// each midnight reset destroyed the previous day's attribution. Two
// projections were built by comparing a remembered figure from one day
// against a live figure from another. Days are now archived before the
// reset, and every call is attributed to a named caller.
// ════════════════════════════════════════════════════════════
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';
import path from 'path';

export const FMP_HISTORY_MAX_DAYS = 120;

export function fmpDayKey(ts = new Date()) {
  return ts.toISOString().slice(0, 10);
}

export function emptyBudget(day = fmpDayKey(), reset_observed_at = null) {
  return {
    day,
    outbound: 0,
    backfill_outbound: 0,
    live_outbound: 0,
    rate_limited: 0,
    reset_observed_at,
    callers: {}
  };
}

export function historyPathFor(budgetPath) {
  return path.join(path.dirname(budgetPath), 'fmp-proxy-history.json');
}

export function readHistory(budgetPath) {
  try {
    const p = historyPathFor(budgetPath);
    if (!existsSync(p)) return [];
    const parsed = JSON.parse(readFileSync(p, 'utf8'));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

// Append a finished day to the history file. Idempotent on day key, so a
// restart that re-reads the same stale file cannot double-count.
export function archiveDay(budgetPath, record) {
  try {
    if (!record || !record.day) return false;
    const hasTraffic = Number(record.outbound || 0) > 0 ||
      Object.keys(record.callers || {}).length > 0;
    if (!hasTraffic) return false;
    const hist = readHistory(budgetPath);
    if (hist.some(h => h && h.day === record.day)) return false;
    hist.push({
      day: record.day,
      outbound: Number(record.outbound || 0),
      backfill_outbound: Number(record.backfill_outbound || 0),
      live_outbound: Number(record.live_outbound || 0),
      rate_limited: Number(record.rate_limited || 0),
      callers: record.callers || {},
      archived_at: new Date().toISOString()
    });
    const trimmed = hist.slice(-FMP_HISTORY_MAX_DAYS);
    const p = historyPathFor(budgetPath);
    mkdirSync(path.dirname(p), { recursive: true });
    writeFileSync(p, JSON.stringify(trimmed, null, 2));
    return true;
  } catch (e) {
    console.error('[fmp-budget] history append failed:', e.message);
    return false;
  }
}

// Load today's budget. If the file holds a previous day, that day is
// archived first — this is the path that runs when the process restarts
// across midnight, which is exactly when the old code lost the data.
export function loadBudget(budgetPath, today = fmpDayKey()) {
  try {
    if (!existsSync(budgetPath)) return emptyBudget(today);
    const parsed = JSON.parse(readFileSync(budgetPath, 'utf8'));
    if (parsed.day !== today) {
      archiveDay(budgetPath, parsed);
      return emptyBudget(today, parsed.reset_observed_at || null);
    }
    return { ...emptyBudget(today, parsed.reset_observed_at || null), ...parsed, callers: parsed.callers || {} };
  } catch (e) {
    console.error('[fmp-budget] load failed:', e.message);
    return emptyBudget(today);
  }
}

export function persistBudget(budgetPath, budget) {
  try {
    mkdirSync(path.dirname(budgetPath), { recursive: true });
    writeFileSync(budgetPath, JSON.stringify(budget, null, 2));
    return true;
  } catch (e) {
    console.error('[fmp-budget] persist failed:', e.message);
    return false;
  }
}

// field is 'requests' (proxy call, may be a cache hit) or 'outbound'
// (an actual upstream FMP request — the one that costs quota).
export function bumpCaller(budget, caller, field) {
  if (!budget || !caller) return budget;
  if (!budget.callers) budget.callers = {};
  const row = budget.callers[caller] || { requests: 0, outbound: 0 };
  row[field] = Number(row[field] || 0) + 1;
  budget.callers[caller] = row;
  return budget;
}
