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
