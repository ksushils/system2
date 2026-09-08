// ══════════════════════════════════════════════════════════════════════════
// REJECTION CLASSIFICATION — read-time only. NOTHING here rewrites a row.
//
// `rejections` conflates two different events: a candidate JUDGED AND DECLINED,
// and the scanner BEING UNABLE TO RUN. Measured 2026-08-22 over 74,411 rows:
// 34.5% of the corpus fleet-wide is infrastructure, and for failed_breakout it
// is 83.6% (FMP_RATE_LIMIT alone is 25,323 of its 30,419). An analysis that
// does not separate them is, for that scanner, a report on FMP's rate limiter.
//
// THREE RULES, learned the hard way:
//   1. INFRA is tested BEFORE decision. An over-broad infra pattern therefore
//      STEALS decisions -- which is why the pattern is `\bfailed\b` and NOT
//      `\bfail\b`: the looser form swallows "rel-strength fail", a real verdict.
//   2. An unmatched reason returns UNCLASSIFIED and NEVER defaults to DECISION.
//      2,915 rows (3.9%) genuinely record no reason -- "rejected", "no signal",
//      "unknown", "RISK_BLOCKED: unspecified". Those must stay uncounted, not
//      be quietly folded into the decision corpus.
//   3. Classify the NORMALISED prefix, not the raw string. "Breakdown 0.00% <
//      min" and "Breakdown 1.2% < min" are one rule, not two.
// ══════════════════════════════════════════════════════════════════════════

export const REJECTION_CLASSES = ['DECISION', 'INFRASTRUCTURE', 'UNCLASSIFIED'];

// The scanner could not evaluate a candidate at all.
const INFRA = [
  /rate[_ ]?limit/, /timeout/, /timed out/, /unreachable/, /econn/, /socket/,
  /not enough .*candle/, /no .*data/, /no watchlist/, /missing/, /unavailable/,
  /\berror\b/, /\bfailed\b/, /exception/, /\bnull\b/, /undefined/, /\bnan\b/,
  /fetch/, /http [45]\d\d/, /no quote/, /no price/, /\bempty\b/, /no universe/,
  /no candles/, /\bapi\b/, /parse/,
  /insufficient\b.*\b(data|bar|bars|candle|candles|history)/,
  /^insufficient data/, /no_config/, /\bconfig\b.*(missing|absent)/
];

// A candidate was judged, or a risk/policy/schedule rule was applied.
const DECISION = [
  /heat/, /concentration/, /breakdown/, /\brsi\b/, /volume/, /\bvol\b/, /\bgap\b/,
  /spread/, /streak/, /pattern/, /vwap/, /score/, /correlation/,
  /max .*position/, /max .*trade/, /max .*order/, /daily loss/, /kill switch/,
  /paper_only/, /slippage/, /liquidity/, /\batr\b/, /\bstop\b/, /\brisk\b/,
  /breaker/, /veto/, /already open/, /duplicate/, /cooldown/, /taper/,
  /reclaim/, /fade/, /too loose/, /too tight/, /too low/, /no pattern/,
  /window/, /before n/, /after n/,
  /outside/, /session/, /hours/, /market closed/, /regime/, /^gate:/,
  /extreme/, /candle quality/, /weak candle/, /level/, /trend/, /momentum/,
  /break n/, /or tests/, /back above/, /current candle/, /adx/, /squeeze/,
  /banned/, /depth/, /quality/, /rel-strength/, /news/, /downgrade/,
  /^score:/, /^spread:/, /^regime:/, /^risk_gate:/, /^system_breaker:/
];

// Collapse interpolated values so one RULE is one prefix.
export function normalizeRejectionReason(reason) {
  let r = String(reason == null ? '' : reason).trim();
  r = r.replace(/\d+(\.\d+)?%?/g, 'N');
  r = r.replace(/\([^)]*\)/g, '()');
  r = r.replace(/\s+/g, ' ');
  return r.slice(0, 70);
}

export function classifyRejectionReason(reason) {
  const prefix = normalizeRejectionReason(reason);
  const low = prefix.toLowerCase();
  for (const re of INFRA)    if (re.test(low)) return 'INFRASTRUCTURE';
  for (const re of DECISION) if (re.test(low)) return 'DECISION';
  return 'UNCLASSIFIED';
}

// Convenience for callers that want both without normalising twice.
export function rejectionClassOf(reason) {
  const prefix = normalizeRejectionReason(reason);
  const low = prefix.toLowerCase();
  let cls = 'UNCLASSIFIED';
  for (const re of INFRA)    { if (re.test(low)) { cls = 'INFRASTRUCTURE'; break; } }
  if (cls === 'UNCLASSIFIED') for (const re of DECISION) { if (re.test(low)) { cls = 'DECISION'; break; } }
  return { prefix, class: cls };
}
