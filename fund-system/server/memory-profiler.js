import { appendFile, stat, rename } from 'fs/promises';
import { monitorEventLoopDelay } from 'perf_hooks';
import v8 from 'v8';

const MB = 1024 * 1024;

function finiteNumber(value) {
  return Number.isFinite(value) ? value : null;
}

export function startMemoryProfiler(app, options = {}) {
  const logPath = options.logPath || '/root/system2-core/logs/fund_memory_profile.jsonl';
  const intervalMs = Number(options.intervalMs || process.env.FUND_MEMORY_PROFILE_INTERVAL_MS || 5000);
  const spikeBytes = Number(options.spikeBytes || process.env.FUND_MEMORY_PROFILE_SPIKE_MB || 850) * MB;
  const maxLogBytes = Number(options.maxLogBytes || process.env.FUND_MEMORY_PROFILE_MAX_LOG_MB || 20) * MB;
  const activeRequests = new Map();
  const activeOperations = new Map();
  let requestSequence = 0;
  let operationSequence = 0;
  let lastCompletedRequest = null;
  let writeChain = Promise.resolve();
  let rotating = false;

  const eventLoop = monitorEventLoopDelay({ resolution: 20 });
  eventLoop.enable();

  const safePath = (req) => String(req.route?.path || req.path || req.originalUrl || '')
    .split('?')[0]
    .slice(0, 180);

  app.use((req, res, next) => {
    const id = ++requestSequence;
    const startedAt = Date.now();
    const item = { id, method: req.method, path: safePath(req), startedAt };
    activeRequests.set(id, item);
    res.on('finish', () => {
      activeRequests.delete(id);
      lastCompletedRequest = {
        method: item.method,
        path: item.path,
        status: res.statusCode,
        duration_ms: Date.now() - startedAt,
        response_bytes: Number(res.getHeader('content-length')) || null,
        at: new Date().toISOString(),
      };
    });
    res.on('close', () => activeRequests.delete(id));
    next();
  });

  const rotateIfNeeded = async () => {
    if (rotating) return;
    rotating = true;
    try {
      const info = await stat(logPath).catch(() => null);
      if (info && info.size >= maxLogBytes) {
        await rename(logPath, `${logPath}.1`).catch(() => {});
      }
    } finally {
      rotating = false;
    }
  };

  const write = (row) => {
    writeChain = writeChain
      .then(rotateIfNeeded)
      .then(() => appendFile(logPath, `${JSON.stringify(row)}\n`, { encoding: 'utf8', mode: 0o600 }))
      .catch((error) => console.error('[memory-profiler] write failed:', error.message));
  };

  const sample = (reason = 'periodic') => {
    const memory = process.memoryUsage();
    const heap = v8.getHeapStatistics();
    const spaces = v8.getHeapSpaceStatistics();
    const row = {
      at: new Date().toISOString(),
      reason,
      rss_mb: finiteNumber(memory.rss / MB),
      heap_used_mb: finiteNumber(memory.heapUsed / MB),
      heap_total_mb: finiteNumber(memory.heapTotal / MB),
      external_mb: finiteNumber(memory.external / MB),
      array_buffers_mb: finiteNumber(memory.arrayBuffers / MB),
      heap_limit_mb: finiteNumber(heap.heap_size_limit / MB),
      event_loop_delay_mean_ms: finiteNumber(eventLoop.mean / 1e6),
      event_loop_delay_max_ms: finiteNumber(eventLoop.max / 1e6),
      active_handle_count: typeof process._getActiveHandles === 'function' ? process._getActiveHandles().length : null,
      active_request_count: activeRequests.size,
      active_requests: [...activeRequests.values()].slice(0, 20).map(({ method, path, startedAt }) => ({
        method, path, duration_ms: Date.now() - startedAt,
      })),
      active_operations: [...activeOperations.values()].slice(0, 20).map(({ name, startedAt }) => ({
        name, duration_ms: Date.now() - startedAt,
      })),
      last_completed_request: lastCompletedRequest,
      heap_spaces_mb: Object.fromEntries(spaces.map((space) => [space.space_name, {
        used: finiteNumber(space.space_used_size / MB),
        available: finiteNumber(space.space_available_size / MB),
        physical: finiteNumber(space.physical_space_size / MB),
      }])),
      spike: memory.rss >= spikeBytes,
    };
    write(row);
    if (row.spike) {
      console.warn(`[memory-profiler] RSS spike ${row.rss_mb.toFixed(1)}MB active=${JSON.stringify(row.active_operations)} requests=${JSON.stringify(row.active_requests)}`);
    }
    eventLoop.reset();
    return row;
  };

  const track = async (name, operation) => {
    const id = ++operationSequence;
    const startedAt = Date.now();
    activeOperations.set(id, { name, startedAt });
    sample(`operation_start:${name}`);
    try {
      return await operation();
    } finally {
      sample(`operation_end:${name}`);
      activeOperations.delete(id);
    }
  };

  const timer = setInterval(() => sample(), intervalMs);
  timer.unref?.();
  sample('startup');
  return { sample, track };
}
