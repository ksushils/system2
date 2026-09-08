export function analyticsFirewallSql(outcomeAlias = '', rejectionAlias = '') {
  const o = outcomeAlias ? `${outcomeAlias}.` : '';
  const r = rejectionAlias ? `${rejectionAlias}.` : '';
  const scanner = outcomeAlias&&rejectionAlias ? `COALESCE(${o}scanner,${r}scanner,'')` : rejectionAlias?`COALESCE(${r}scanner,'')`:`COALESCE(${o}scanner,'')`;
  const ticker = outcomeAlias&&rejectionAlias ? `COALESCE(${o}ticker,${r}ticker,${r}data->>'ticker','')` : rejectionAlias?`COALESCE(${r}ticker,${r}data->>'ticker','')`:`COALESCE(${o}ticker,'')`;
  const eventTs = outcomeAlias&&rejectionAlias ? `COALESCE(${o}event_ts,NULLIF(${r}data->>'rejected_time','')::timestamptz,NULLIF(${r}data->>'ts','')::timestamptz)` : rejectionAlias?`COALESCE(NULLIF(${r}data->>'rejected_time','')::timestamptz,NULLIF(${r}data->>'ts','')::timestamptz)`:`${o}event_ts`;
  const systemBreaker = rejectionAlias ? `AND COALESCE(${r}data->>'reason','') NOT LIKE 'SYSTEM_BREAKER:%'` : '';
  return `
    ${scanner} NOT IN ('test','test_harness')
    AND ${ticker} NOT LIKE 'ZZ%'
    AND NOT (${scanner}='pa' AND ${eventTs} >= '2026-08-03T14:00:04Z'::timestamptz AND ${eventTs} <= '2026-08-03T22:39:41Z'::timestamptz)
    ${systemBreaker}`;
}

// ── IN-MEMORY MIRROR ────────────────────────────────────────────────────────
// db.data.trade_brain is a plain persisted ARRAY, not a table, so the SQL
// predicate above can never reach it. This is the same firewall for in-memory
// collections. It mirrors the two STRUCTURAL rules only:
//     scanner NOT IN ('test','test_harness')   and   ticker NOT LIKE 'ZZ%'
// It deliberately does NOT mirror the pa 2026-08-03 window clause. That clause
// keys on the EVENT timestamp; a brain row carries only `recorded_at`, which is
// the WRITE time. Matching a time window on the wrong timestamp is how a filter
// silently excludes the wrong rows, so it is better omitted than approximated.
// There are currently zero pa rows in the brain, so the omission excludes
// nothing today; if pa ever records one, this needs a real event time.
// SEED ROWS, excluded by IDENTITY rather than by status.
// trades 1-4 (fmp, 2025-01-08 .. 2025-01-15) are the first four rows the system
// ever held. They use LONG/SHORT where every real trade writes BUY/SELL, and
// there is a 16-month gap between them and the next genuine trade (2026-05-19).
// They were kept off the fleet board only by calcFundStats' `status==='CLOSED'`
// filter, which is not a firewall: any status change readmits them, and they are
// the ONLY two positives among fmp's 7 pnl-bearing rows, where all four real fmp
// results are losses. Excluding them by id makes that structural.
// Checked before adding: no trade_brain, rejections or signals row carries an id
// in 1..4 (brain ids are `brain_<ts>_<rand>`), so this cannot catch anything else.
const SEED_TRADE_IDS = new Set(['1', '2', '3', '4']);

export function analyticsFirewallRow(row = {}) {
  const scanner = String(row.scanner ?? '');
  if (scanner === 'test' || scanner === 'test_harness') return false;
  if (String(row.ticker ?? '').startsWith('ZZ')) return false;
  if (SEED_TRADE_IDS.has(String(row.id ?? ''))) return false;
  return true;
}
