#!/usr/bin/env python3
"""Patch exported n8n node definitions for operational attribution only.

Input/output files contain live workflow data and must remain outside Git.
"""
import argparse
import json
from pathlib import Path


RISK_AUDIT = r'''
async function retainRisk(t, terminalState, reason, verdict, downstreamState) {
  const symbol = String(t.ticker || t.symbol || t.epic || t.cap_epic || t.pair || 'UNKNOWN');
  const body = {
    terminal_state: terminalState, symbol,
    input_signal_id: t.signal_id || t.signalId || t.id ||
      [symbol, t.signal_ts || t.ts || t.timestamp || 'NO_SOURCE_TIMESTAMP', t.entry ?? t.entry_price ?? 'NO_ENTRY'].join(':'),
    signal_timestamp: t.signal_ts || t.ts || t.timestamp || null,
    entry: t.entry ?? t.entry_price ?? null,
    stop: t.sl ?? t.stop_loss ?? t.stop ?? null,
    target: t.tp1 ?? t.take_profit_1 ?? t.tp ?? null,
    qty: t.qty ?? t.size ?? t.quantity ?? null,
    heat: t.projected_heat_pct ?? t.heat_pct ?? null,
    risk_inputs: {
      risk_amount: t.risk_usd ?? t.risk_amount ?? t.riskAmount ?? null,
      projected_heat_pct: t.projected_heat_pct ?? t.heat_pct ?? null,
      effective_heat_pct: verdict?.effective_heat_pct ?? null
    },
    risk_reason: reason, downstream_order_state: downstreamState
  };
  try {
    if (!KEY) throw new Error('SCANNER_KEY_UNAVAILABLE');
    const response = await this.helpers.httpRequest({method:'POST', url:BASE+'/api/pa/risk-attribution',
      headers:H, body, json:true, ignoreHttpStatusErrors:true, timeout:5000, returnFullResponse:true});
    if (!response || response.statusCode < 200 || response.statusCode >= 300)
      throw new Error('ATTRIBUTION_HTTP_'+(response?.statusCode ?? 'UNKNOWN'));
    return true;
  } catch (error) {
    // Workflow static data is a retained fallback when the dashboard is down.
    // Never permit a risk PASS to reach an order without durable attribution.
    sd.paRiskAttributionOutbox = sd.paRiskAttributionOutbox || [];
    sd.paRiskAttributionOutbox.push({...body,
      terminal_state: terminalState === 'RISK_PASS' ? 'RISK_ERROR' : terminalState,
      downstream_order_state: 'NO_ORDER',
      audit_error:String(error?.message || error).slice(0,160), retained_at:new Date().toISOString()});
    if (sd.paRiskAttributionOutbox.length > 1000) sd.paRiskAttributionOutbox.shift();
    console.log('[PA RISK ATTRIBUTION FALLBACK] '+symbol+' '+terminalState+' '+String(error?.message || error).slice(0,100));
    return false;
  }
}
'''


def patch_pa(source: str) -> str:
    if 'async function retainRisk(' in source:
        raise ValueError('already patched')
    needle = "if (!KEY) { console.log('[GATE KEY GUARD] no scanner key in staticData — config node must run first; emitting nothing'); return []; }"
    replacement = "if (!KEY) { for (const item of $input.all()) await retainRisk.call(this, item.json || {}, 'RISK_EMPTY_OUTPUT', 'SCANNER_KEY_UNAVAILABLE', null, 'NO_ORDER'); return []; }"
    assert source.count(needle) == 1
    source = source.replace(needle, RISK_AUDIT + '\n' + replacement)
    replacements = [
        ("await logReject.call(this, t, 'DATA_QUALITY: missing ticker', JSON.stringify(Object.keys(t).slice(0,15)), null);",
         "await retainRisk.call(this, t, 'RISK_REJECT', 'DATA_QUALITY: missing ticker', null, 'NO_ORDER');\n    await logReject.call(this, t, 'DATA_QUALITY: missing ticker', JSON.stringify(Object.keys(t).slice(0,15)), null);"),
        ("await logReject.call(this, t, 'DATA_QUALITY: invalid entry price', 'entry=' + String(t.entry ?? t.entry_price), null);",
         "await retainRisk.call(this, t, 'RISK_REJECT', 'DATA_QUALITY: invalid entry price', null, 'NO_ORDER');\n    await logReject.call(this, t, 'DATA_QUALITY: invalid entry price', 'entry=' + String(t.entry ?? t.entry_price), null);"),
        ("  let v = null;\n  try {",
         "  let v = null;\n  let riskRequestError = false;\n  try {"),
        ("  } catch (e) {\n    v = { allowed: false, reason: 'RISK_CHECK_UNREACHABLE: ' + (e.message || e) };\n  }",
         "  } catch (e) {\n    riskRequestError = true;\n    v = { allowed: false, reason: 'RISK_CHECK_UNREACHABLE: ' + (e.message || e) };\n  }"),
        ("    await logReject.call(this, t, 'RISK_GATE: ' + reason, null, v);",
         "    await retainRisk.call(this, t, riskRequestError ? 'RISK_ERROR' : 'RISK_REJECT', reason, v, 'NO_ORDER');\n    await logReject.call(this, t, 'RISK_GATE: ' + reason, null, v);"),
        ("    await logReject.call(this, t, 'RISK_GATE: paper_only — live order suppressed', null, v);",
         "    await retainRisk.call(this, t, 'RISK_REJECT', 'paper_only — live order suppressed', v, 'NO_ORDER');\n    await logReject.call(this, t, 'RISK_GATE: paper_only — live order suppressed', null, v);"),
        ("  out.push({ json: { ...t, risk_allowed: true, paper_only: false,",
         "  if (!await retainRisk.call(this, t, 'RISK_PASS', 'allowed', v, 'SENT_TO_ORDER_NODE')) continue;\n  out.push({ json: { ...t, risk_allowed: true, paper_only: false,"),
    ]
    for old, new in replacements:
        if source.count(old) != 1:
            raise ValueError(f'PA patch anchor count {source.count(old)}: {old[:70]}')
        source = source.replace(old, new)
    return source


def patch_indices(source: str) -> str:
    old = "skip_reason:`Score ${finalScore} below min ${MIN_SCORE} (${sig} ${dir})`,"
    new = "skip_reason:`${sig} score ${finalScore} below effective minimum ${effectiveMin}; global fallback ${MIN_SCORE} (${dir})`,"
    if source.count(old) != 1:
        raise ValueError(f'indices patch anchor count {source.count(old)}')
    return source.replace(old, new)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('workflow', choices=('pa', 'indices'))
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    nodes = json.loads(args.source.read_text(encoding='utf-8'))
    name = '🛡️ Risk Gate: PA Momentum' if args.workflow == 'pa' else 'Indices Signal Engine'
    selected = [node for node in nodes if node.get('name') == name]
    if len(selected) != 1:
        raise ValueError(f'expected one node: {name}')
    original = selected[0]['parameters']['jsCode']
    selected[0]['parameters']['jsCode'] = patch_pa(original) if args.workflow == 'pa' else patch_indices(original)
    args.destination.write_text(json.dumps(nodes, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'workflow': args.workflow, 'node': name, 'before_chars': len(original), 'after_chars': len(selected[0]['parameters']['jsCode'])}))


if __name__ == '__main__':
    main()
