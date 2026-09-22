import assert from 'node:assert/strict';
import { reconcileAbsentCapitalTrade } from './position-watchdog.js';

const trade = { scanner: 'volume', deal_id: 'deal-1', ticker: 'MRK' };
const requests = [];
const result = await reconcileAbsentCapitalTrade(trade, {
  scannerKey: 'fixture-only',
  fetchImpl: async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => ({ status: 'ok', found: true, risk_released: { ok: true, released: 1 } }) };
  },
});
assert.equal(result.closed, true);
assert.equal(requests.length, 1);
assert.equal(requests[0].url, 'http://127.0.0.1:3210/api/trade/close');
assert.equal(JSON.parse(requests[0].options.body).action, 'FULL_EXIT');
assert.equal(requests[0].options.method, 'POST');

const missingEvidence = await reconcileAbsentCapitalTrade(trade, {
  scannerKey: 'fixture-only',
  fetchImpl: async () => ({ ok: false, status: 422, json: async () => ({ reason: 'CLOSE_ECONOMICS_UNRESOLVED' }) }),
});
assert.equal(missingEvidence.closed, false);
assert.equal(missingEvidence.reason, 'CLOSE_ECONOMICS_UNRESOLVED');
console.log('position absence recovery uses the authoritative close route; no broker-order endpoint exercised');
