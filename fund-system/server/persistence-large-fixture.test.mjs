// Isolated benchmark only. It never opens the live server or writes production data.
import { readFile, rename, unlink, writeFile } from 'node:fs/promises';
import { basename, join } from 'node:path';
import { tmpdir } from 'node:os';

const [mode = 'compact', fixture] = process.argv.slice(2);
if (!fixture || !['old', 'compact'].includes(mode)) {
  throw new Error('usage: node --expose-gc persistence-large-fixture.test.mjs old|compact /path/to/fund.json');
}
const bytes = 1024 * 1024;
const mb = value => Number((value / bytes).toFixed(1));
const state = JSON.parse(await readFile(fixture, 'utf8'));
global.gc?.();
const before = process.memoryUsage();
let eventLoopLagMs = 0;
const probe = new Promise(resolve => {
  const scheduled = performance.now();
  setTimeout(() => { eventLoopLagMs = performance.now() - scheduled; resolve(); }, 0);
});
const started = performance.now();
const serialized = mode === 'old' ? JSON.stringify(state, null, 2) : JSON.stringify(state);
const afterSerialize = process.memoryUsage();
const temp = join(tmpdir(), `system2-${basename(fixture)}-${mode}-${process.pid}.tmp`);
await writeFile(temp, serialized, 'utf8');
await rename(temp, `${temp}.done`);
await unlink(`${temp}.done`);
await probe;
const after = process.memoryUsage();
console.log(JSON.stringify({
  mode, fixture_bytes: (await readFile(fixture)).byteLength, serialized_bytes: Buffer.byteLength(serialized),
  duration_ms: Number((performance.now() - started).toFixed(1)), event_loop_lag_ms: Number(eventLoopLagMs.toFixed(1)),
  rss_before_mb: mb(before.rss), rss_after_serialize_mb: mb(afterSerialize.rss), rss_after_mb: mb(after.rss),
  heap_before_mb: mb(before.heapUsed), heap_after_serialize_mb: mb(afterSerialize.heapUsed), heap_after_mb: mb(after.heapUsed),
}));
