import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { analyticsFirewallRow } from '../server/analytics-firewall.js';

test('analytics firewall excludes fixtures and invalid rows but retains production rows', () => {
  assert.equal(analyticsFirewallRow({ id: '20', scanner: 'test_harness', ticker: 'AAPL' }), false);
  assert.equal(analyticsFirewallRow({ id: '21', scanner: 'momentum', ticker: 'ZZINVALID' }), false);
  assert.equal(analyticsFirewallRow({ id: '22', scanner: 'momentum', ticker: 'AAPL' }), true);
});

test('Layer1 outcome backlog applies the canonical SQL firewall to every population', () => {
  const source = fs.readFileSync(new URL('../server/layer1.js', import.meta.url), 'utf8');
  const fn = source.slice(source.indexOf('export async function outcomeBacklogCounts'), source.indexOf('export async function unlabeledOutcomeCount'));
  assert.match(fn, /FROM signals s\s+WHERE \$\{analyticsFirewallSql\('','s'\)\}/);
  assert.match(fn, /FROM rejections r WHERE \$\{analyticsFirewallSql\('','r'\)\}/);
  assert.match(fn, /FROM signal_outcomes o[\s\S]*analyticsFirewallSql\('o'\)/);
  assert.equal((fn.match(/analyticsFirewallSql\(/g) || []).length, 5);
});
