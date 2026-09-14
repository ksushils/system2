'use strict';
const assert = require('assert/strict');
const { evaluateInvariant } = require('./pmf-retirement-invariant.cjs');
const blockedSelfTest = async () => ({ self_test: { primary: 'retired_blocked', pmf_late: 'retired_blocked', retry: 'retired_blocked', manual: 'retired_blocked', broker_calls: 0 } });
(async () => {
  const falseNegative = await evaluateInvariant({ desiredValues: { PMF_AUTO_EXEC_ENABLED: 'false', PMF_V1_RETIRED_NO_NEW_ENTRIES: 'true' }, effectiveValues: { PMF_AUTO_EXEC_ENABLED: 'true', PMF_V1_RETIRED_NO_NEW_ENTRIES: 'true' }, targetPid: 999, runSelfTest: blockedSelfTest });
  assert.equal(falseNegative.ok, false); assert.equal(falseNegative.status, 'PMF_RETIREMENT_INVARIANT_BROKEN');
  const healthy = await evaluateInvariant({ desiredValues: { PMF_AUTO_EXEC_ENABLED: 'false', PMF_V1_RETIRED_NO_NEW_ENTRIES: 'true' }, effectiveValues: { PMF_AUTO_EXEC_ENABLED: 'false', PMF_V1_RETIRED_NO_NEW_ENTRIES: 'true' }, targetPid: 1000, runSelfTest: blockedSelfTest });
  assert.equal(healthy.ok, true); assert.equal(healthy.status, 'HEALTHY'); assert.equal(healthy.self_test.broker_calls, 0);
  console.log(JSON.stringify({ ok: true, false_negative_fixture: falseNegative.status, healthy_fixture: healthy.status, broker_calls: 0 }));
})().catch(error => { console.error(error); process.exit(1); });
