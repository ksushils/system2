import assert from 'node:assert/strict';
import { createPersistenceController } from './persistence-controller.js';

let concurrent = 0;
let maxConcurrent = 0;
let completed = [];
let failNext = false;
const telemetry = [];
const controller = createPersistenceController({
  onTelemetry: row => telemetry.push(row),
  runCycle: async ({ cycle, requiresPostgres, coalescedRequests }) => {
    concurrent += 1;
    maxConcurrent = Math.max(maxConcurrent, concurrent);
    await new Promise(resolve => setTimeout(resolve, 15));
    concurrent -= 1;
    if (failNext) { failNext = false; throw new Error('fixture postgres failure'); }
    completed.push({ cycle, requiresPostgres, coalescedRequests });
    return { serializedJsonBytes: 142 * 1024 * 1024, postgresDurationMs: 10, jsonWriteDurationMs: 5 };
  },
});

// A: one request; B/C: simultaneous and burst requests; G: one dirty follow-up.
await controller.request({ requiresPostgres: true });
const burst = [controller.request(), controller.request({ requiresPostgres: true })];
for (let i = 0; i < 8; i += 1) burst.push(controller.request());
await Promise.all(burst);
assert.equal(maxConcurrent, 1);
assert.ok(completed.some(c => c.coalescedRequests >= 2));

// E/F failure: lock releases, retained demand is served by the next explicit request.
failNext = true;
await assert.rejects(controller.request({ requiresPostgres: true }), /fixture postgres failure/);
await controller.request();
assert.equal(maxConcurrent, 1);
assert.ok(telemetry.some(t => t.error));
console.log(JSON.stringify({ ok: true, max_concurrent_full_saves: maxConcurrent, cycles: completed.length, telemetry_rows: telemetry.length }));
