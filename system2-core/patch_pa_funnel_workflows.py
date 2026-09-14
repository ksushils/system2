#!/usr/bin/env python3
"""Patch exported n8n workflows for the PA funnel audit without changing strategy rules."""

import json
import sys
from pathlib import Path


def load_export(path: Path):
    payload = json.loads(path.read_text())
    return payload, payload[0] if isinstance(payload, list) else payload


def node(workflow, name):
    matches = [item for item in workflow["nodes"] if item.get("name") == name]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one node named {name!r}, found {len(matches)}")
    return matches[0]


def replace_once(text, old, new, label):
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected one match, found {text.count(old)}")
    return text.replace(old, new, 1)


def patch_pa(workflow):
    login = node(workflow, "Capital.com Login")
    login["executeOnce"] = True

    heat = node(workflow, "Heat + Slippage Check")
    code = heat["parameters"]["jsCode"]
    start = code.index("// Slippage check")
    code = code[:start] + """// Preserve every confirmed signal while reusing the one batch login response.
const signals = $('IF Signal Valid').all(0, 0)
  .map(item => item.json || {})
  .filter(signal => signal.pass === true);

return signals.map((signal, index) => {
  const entryPrice  = parseFloat(signal.entry || signal.price || 0);
  const currentPrice= parseFloat(signal.price || 0);
  const slippagePct = (entryPrice > 0 && currentPrice > 0)
    ? Math.abs(currentPrice - entryPrice) / entryPrice * 100
    : 0;
  const newMarginEstimate = (signal.posValue || 0) / leverageGuess;
  const projectedHeat = ACCT > 0 ? (effectiveExp + newMarginEstimate) / ACCT : 0;
  const heatOk = projectedHeat < MAX_HEAT;
  const slippageOk = slippagePct <= 1.5;

  return { json: {
    ...signal,
    cst,
    secToken,
    portfolioHeat: +heat.toFixed(4),
    projectedHeat: +projectedHeat.toFixed(4),
    heatOk,
    slippageOk,
    slippagePct: +slippagePct.toFixed(3),
    openPositions: positions.length,
    totalMargin: +totalMargin.toFixed(2),
    notional: +notional.toFixed(2),
    effectiveExp: +effectiveExp.toFixed(2),
    funnel_stage: 'HEAT_SLIPPAGE',
    funnel_outcome: heatOk && slippageOk ? 'PASS' : (!heatOk ? 'HEAT_REJECT' : 'SLIPPAGE_REJECT')
  }, pairedItem: { item: index } };
});
"""
    heat["parameters"]["jsCode"] = code

    detector = node(workflow, "Streak + RSI")
    code = detector["parameters"]["jsCode"]
    code = replace_once(
        code,
        "function reject(reason,rsi=0) {",
        "let diagnostic = {};\nfunction reject(reason,rsi=0) {",
        "diagnostic declaration",
    )
    code = replace_once(
        code,
        "    vwap:+vwap.toFixed(2),vwapPct:0\n",
        "    vwap:+vwap.toFixed(2),vwapPct:0,diagnostic\n",
        "rejection diagnostic",
    )
    marker = "const bullRsiOk = rsi>=45 && rsi<=78;"
    diagnostic = marker + """

diagnostic = {
  bar_interval_minutes: candleMinutes,
  pole_volume_values: poleCandles.map(c => parseInt(c.volume||0)),
  flag_volume_values: flagCandles.map(c => parseInt(c.volume||0)),
  avg_pole_volume: +avgPoleVol.toFixed(3),
  avg_flag_volume: +avgFlagVol.toFixed(3),
  taper_multiplier: 1.5,
  volume_tapering_ok: volTapering,
  breakout_volume: parseInt(current.volume||0),
  breakout_volume_threshold: +(recentAvgVol*1.2).toFixed(3),
  breakout_volume_ok: breakoutVolOk,
  effective_pole_pct: +effectivePolePct.toFixed(4),
  minimum_flagpole_pct: minFlagpole,
  flag_range_pct: +flagRange.toFixed(4),
  maximum_flag_range_pct: maxFlagRange,
  above_vwap_ok: vwap<=0 || curClose>=vwap,
  distance_from_high_pct: +((dayHigh-curClose)/dayHigh*100).toFixed(4),
  distance_from_high_ok: (dayHigh-curClose)/dayHigh*100 <= 8.0,
  bull_rsi_ok: bullRsiOk,
  pole_age_minutes: minutesSincePole,
  pole_age_ok: poleAge,
  bull_flag_breakout_proximity_ok: candles.slice(n-5,n).some(c=>parseFloat(c.close)>=flagHigh*0.995)
};"""
    code = replace_once(code, marker, diagnostic, "pattern diagnostic block")
    code = replace_once(
        code,
        "  volClimax:       +(parseInt(current.volume||0)/recentAvgVol).toFixed(2)\n",
        "  volClimax:       +(parseInt(current.volume||0)/recentAvgVol).toFixed(2),\n  diagnostic\n",
        "successful-signal diagnostic",
    )
    detector["parameters"]["jsCode"] = code

    telegram = node(workflow, "Telegram P1.5 Update")
    body = telegram["parameters"]["jsonBody"]
    body = replace_once(body, "Added: {{ $json.added }} new stocks", "Actually added: {{ $json.added }}", "watchlist added label")
    body = replace_once(body, "New: {{ $json.newSymbols || 'None' }}", "Discovered symbols: {{ $json.newSymbols || 'None' }}", "watchlist discovered label")
    telegram["parameters"]["jsonBody"] = body


def patch_indices(workflow):
    engine = node(workflow, "Indices Signal Engine")
    code = engine["parameters"]["jsCode"]
    old = """    skip_reason:`Score ${finalScore} below min ${MIN_SCORE} (${sig} ${dir})`,
    rejection_stage:'SCORE_TOO_LOW',
    signal_score:finalScore,"""
    new = """    skip_reason:`Raw score ${score} (display ${finalScore}) below strategy min ${effectiveMin}; global fallback ${MIN_SCORE} (${sig} ${dir})`,
    rejection_stage:'SCORE_TOO_LOW',
    raw_score:score,
    display_score:finalScore,
    effective_strategy_min:effectiveMin,
    global_fallback_min:MIN_SCORE,
    applied_score_modifiers:[...reasons],
    signal_score:finalScore,"""
    engine["parameters"]["jsCode"] = replace_once(code, old, new, "indices rejection telemetry")


def main():
    if len(sys.argv) != 5:
        raise SystemExit("usage: patch_pa_funnel_workflows.py PA_IN PA_OUT INDICES_IN INDICES_OUT")
    pa_payload, pa = load_export(Path(sys.argv[1]))
    idx_payload, idx = load_export(Path(sys.argv[3]))
    patch_pa(pa)
    patch_indices(idx)
    Path(sys.argv[2]).write_text(json.dumps(pa_payload, indent=2) + "\n")
    Path(sys.argv[4]).write_text(json.dumps(idx_payload, indent=2) + "\n")
    print(json.dumps({"ok": True, "pa": pa.get("id"), "indices": idx.get("id"), "secrets_printed": False}))


if __name__ == "__main__":
    main()
