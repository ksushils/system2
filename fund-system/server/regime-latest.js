// ════════════════════════════════════════════════════════════
// GET /api/regime/latest — response shaping
//
// Pure: takes the newest regime_snapshots row (or null) and returns the
// status + body. No DB, no clock of its own, so both the "no snapshot"
// and the staleness boundary can be unit-tested exactly.
//
// Never fabricates. If there is no snapshot the answer is 503, not a
// zeroed row — a scanner that reads an invented regime is worse off
// than one that knows the regime is unavailable.
// ════════════════════════════════════════════════════════════

export const REGIME_STALE_AFTER_MIN = 45;

export function shapeRegimeLatest(row, now = new Date()) {
  if (!row) {
    return {
      status: 503,
      body: {
        error: 'NO_REGIME_SNAPSHOT',
        message: 'No regime snapshot exists yet; Layer 4 has not written one.',
        stale_after_minutes: REGIME_STALE_AFTER_MIN
      }
    };
  }

  const ts = row.ts instanceof Date ? row.ts : new Date(row.ts);
  if (!Number.isFinite(ts.getTime())) {
    return {
      status: 503,
      body: {
        error: 'REGIME_SNAPSHOT_UNREADABLE',
        message: 'Newest regime snapshot has an unreadable timestamp.',
        id: row.id ?? null
      }
    };
  }

  const ageMinutes = +(((now.getTime() - ts.getTime()) / 60000).toFixed(2));

  return {
    status: 200,
    body: {
      id: row.id ?? null,
      ts: ts.toISOString(),
      vix: row.vix ?? null,
      spy_price: row.spy_price ?? null,
      spy_vs_20d_pct: row.spy_vs_20d_pct ?? null,
      spy_trend: row.spy_trend ?? null,
      btc_24h_pct: row.btc_24h_pct ?? null,
      dxy: row.dxy ?? null,
      session_hour_et: row.session_hour_et ?? null,
      day_of_week: row.day_of_week ?? null,
      age_minutes: ageMinutes,
      stale: ageMinutes > REGIME_STALE_AFTER_MIN,
      stale_after_minutes: REGIME_STALE_AFTER_MIN
    }
  };
}
