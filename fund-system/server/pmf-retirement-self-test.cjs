'use strict';

const assert = require('assert');
const executor = require('./pmf-auto-executor.cjs');

const env = {
  PMF_AUTO_EXEC_ENABLED: 'false',
  PMF_V1_RETIRED_NO_NEW_ENTRIES: 'true',
  ALPACA_PAPER_BASE_URL: 'https://paper-api.alpaca.markets',
  ALPACA_PAPER_API_KEY: 'TEST_ONLY',
  ALPACA_PAPER_API_SECRET: 'TEST_ONLY',
};
const readEnvValue = name => env[name] || null;
const idea = { id:'retirement-test', ticker:'TEST', entry:100, stopLoss:98, target:104, atr14:2, pmf_price_at_stamp:103 };

(async () => {
  const primary = await executor.maybeExecuteConfirmedPmfs({ ideas:[{...idea}], allIdeas:[], readEnvValue, dryRun:true });
  const late = await executor.maybeExecuteConfirmedPmfs({ ideas:[{...idea, pmf_cohort:'PMF_LATE'}], allIdeas:[], readEnvValue, dryRun:true });
  const retry = await executor.maybeExecuteConfirmedPmfs({ ideas:[{...idea, auto_exec_status:'failed'}], allIdeas:[], readEnvValue, dryRun:true });
  const manual = executor.dryRunPreview({...idea, requested_source:'manual_dashboard'}, readEnvValue);
  for (const result of [primary,late,retry,manual]) {
    assert.equal(result.action,'retired_blocked');
    assert.equal(result.reason,'PMF_V1_RETIRED_NO_NEW_ENTRIES');
    assert.equal(result.broker_calls,0);
  }
  assert.equal(executor.loadConfig(readEnvValue).enabled,false);
  assert.equal(executor.loadConfig(readEnvValue).retiredNoNewEntries,true);
  console.log(JSON.stringify({ok:true,primary:primary.action,pmf_late:late.action,retry:retry.action,manual:manual.action,broker_new_entry_calls:0,reason:primary.reason},null,2));
})().catch(error => { console.error(error.stack); process.exit(1); });
