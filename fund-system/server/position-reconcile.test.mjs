import assert from 'node:assert/strict';
import { reconcilePositions } from './position-reconcile.js';

const now = new Date('2026-09-15T12:00:00Z');
const updatedAt = '2026-09-15T11:59:00Z';
const trade = { id: 1, ticker: 'AAA', direction: 'BUY', size: 1, status: 'OPEN', opened_at: '2026-09-14T12:00:00Z' };
const broker = { market: { epic: 'AAA' }, position: { direction: 'BUY', size: 1, stopLevel: 9 } };
const run = (trades, livePositions) => reconcilePositions({ trades, livePositions, updatedAt, now });

assert.deepEqual(run([trade], [broker]).issues, []);
assert.deepEqual(run([], [broker]).issues, ['untracked_broker_positions']);
assert.deepEqual(run([trade], []).issues, ['phantom_book_positions']);
assert.deepEqual(run([{ ...trade, status: 'CLOSED' }], []).issues, []);
assert.deepEqual(run([{ ticker: 'MODEL', status: 'WATCHING', paper_entry_price: 10 }], []).issues, []);
console.log('PASS position reconciliation fixtures');
