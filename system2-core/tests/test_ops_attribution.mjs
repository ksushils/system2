import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const [paFile, indicesFile] = process.argv.slice(2);
if (!paFile || !indicesFile) throw new Error('usage: test_ops_attribution.mjs pa-nodes.json indices-nodes.json');
const code = (file, name) => JSON.parse(fs.readFileSync(file, 'utf8')).find(node => node.name === name).parameters.jsCode;
const pa = code(paFile, '🛡️ Risk Gate: PA Momentum');
const indices = code(indicesFile, 'Indices Signal Engine');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction(pa);
new AsyncFunction(indices);

// Heat passed upstream, but the canonical risk node has no key and emits zero
// order-path items. The signal must still retain RISK_EMPTY_OUTPUT.
const staticData = { config: { DASHBOARD_URL: 'http://fixture.invalid' } };
const calls = [];
const context = {
  $getWorkflowStaticData: () => staticData,
  $env: {},
  $input: { all: () => [{ json: { symbol: 'GLOO', signal_id: 'fixture-zero-output', entry: 10, sl: 9, tp1: 12, qty: 1, projected_heat_pct: 8.7 } }] },
  console: { log: () => {} },
};
const run = vm.runInNewContext('(async function(){' + pa + '\n})', context);
const output = await run.call({ helpers: { httpRequest: async arg => { calls.push(arg); return { statusCode: 200 }; } } });
assert.equal(output.length, 0);
assert.equal(calls.length, 0);
assert.equal(staticData.paRiskAttributionOutbox.length, 1);
assert.equal(staticData.paRiskAttributionOutbox[0].terminal_state, 'RISK_EMPTY_OUTPUT');
assert.equal(staticData.paRiskAttributionOutbox[0].downstream_order_state, 'NO_ORDER');

staticData.config.SCANNER_API_KEY = 'fixture-only';
const passCalls = [];
const passed = await run.call({ helpers: { httpRequest: async arg => {
  passCalls.push(arg);
  if (arg.url.endsWith('/api/risk/check')) return { allowed: true, paper_only: false, effective_heat_pct: 8.7 };
  if (arg.url.endsWith('/api/pa/risk-attribution')) return { statusCode: 200 };
  throw new Error('unexpected request');
} } });
assert.equal(passed.length, 1);
assert.equal(passCalls.filter(call => call.url.endsWith('/api/pa/risk-attribution')).length, 1);
assert.equal(passCalls.find(call => call.url.endsWith('/api/pa/risk-attribution')).body.terminal_state, 'RISK_PASS');
assert.equal(passCalls.filter(call => /broker|order/.test(call.url)).length, 0);

const expected = {
  TREND_CONTINUATION: 55, EMA_PULLBACK_CONTINUATION: 50, SESSION_BREAKOUT: 50,
  MEAN_REVERSION: 55, VWAP_RECLAIM: 50, TREND_STATE: 52,
};
for (const [strategy, min] of Object.entries(expected)) {
  assert.match(indices, new RegExp(strategy + ':\\s*' + min + '\\b'));
  const display = `${strategy} score ${min - 1} below effective minimum ${min}; global fallback 40 (BUY)`;
  assert.match(display, new RegExp(`effective minimum ${min}`));
}
assert.match(indices, /below effective minimum \$\{effectiveMin\}/);
assert.doesNotMatch(indices, /below min \$\{MIN_SCORE\}/);
console.log('PA zero-output and pass attribution fixtures PASS; broker calls 0; indices six effective minima display fixture PASS');
