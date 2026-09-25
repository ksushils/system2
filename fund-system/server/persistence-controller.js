// Serializes full-state persistence without taking a snapshot or cloning db.data.
// A request made while a cycle is in progress is deliberately coalesced into one
// follow-up cycle, which always reads the latest live state.
export function createPersistenceController({ runCycle, onTelemetry = () => {} }) {
  let active = false;
  let dirty = false;
  let pendingPostgres = false;
  let pendingRequests = 0;
  let drainPromise = null;
  let cycleNumber = 0;

  const drain = async () => {
    let lastResult;
    try {
      while (dirty) {
        dirty = false;
        const requiresPostgres = pendingPostgres;
        pendingPostgres = false;
        const coalescedRequests = pendingRequests;
        pendingRequests = 0;
        const cycle = ++cycleNumber;
        const startedAt = Date.now();
        const before = process.memoryUsage();
        let result;
        let error = null;
        try {
          result = await runCycle({ cycle, requiresPostgres, coalescedRequests });
          lastResult = result;
        } catch (err) {
          error = err;
          // Preserve the demand for a later, explicit request; do not spin.
          dirty = true;
          pendingPostgres ||= requiresPostgres;
          throw err;
        } finally {
          const after = process.memoryUsage();
          onTelemetry({
            at: new Date().toISOString(), cycle, requires_postgres: requiresPostgres,
            coalesced_requests: coalescedRequests, dirty_follow_up: dirty,
            duration_ms: Date.now() - startedAt,
            rss_before_mb: before.rss / 1048576, rss_after_mb: after.rss / 1048576,
            heap_before_mb: before.heapUsed / 1048576, heap_after_mb: after.heapUsed / 1048576,
            external_before_mb: before.external / 1048576, external_after_mb: after.external / 1048576,
            serialized_json_bytes: result?.serializedJsonBytes ?? null,
            postgres_duration_ms: result?.postgresDurationMs ?? null,
            json_write_duration_ms: result?.jsonWriteDurationMs ?? null,
            error: error ? String(error.message || error).slice(0, 300) : null,
          });
        }
      }
      return lastResult;
    } finally {
      active = false;
      drainPromise = null;
    }
  };

  const request = ({ requiresPostgres = false } = {}) => {
    dirty = true;
    pendingPostgres ||= requiresPostgres;
    pendingRequests += 1;
    if (!active) {
      active = true;
      drainPromise = drain();
    }
    return drainPromise;
  };

  return {
    request,
    state: () => ({ active, dirty, pendingPostgres, pendingRequests, cycleNumber }),
  };
}
