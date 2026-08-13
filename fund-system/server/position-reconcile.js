// ════════════════════════════════════════════════════════════
// POSITION RECONCILIATION — by EPIC, not by count.
//
// The old check was `tradeOpen !== liveOpen`, a comparison of two
// cardinalities. That failed in both directions:
//
//   * it MASKED an untracked, unstopped RKLB position for five days,
//     because our open-trade count happened to equal the broker's
//     managed count;
//   * it then FALSELY ALARMED when a stale cache still held a US500
//     position the broker had already closed.
//
// A count comparison collapses "untracked", "phantom" and "drifted"
// into one boolean that coincidence can satisfy. Set difference cannot.
//
// It also refuses to compare at all when the snapshot is stale: a dead
// feed must never again be indistinguishable from a real discrepancy.
// db.data.live_positions is written only by POST /api/positions/live,
// documented as "updated by n8n every 60s".
//
// Pure — no clock of its own, no IO — so every branch is testable.
// ════════════════════════════════════════════════════════════

// 10x the documented 60s refresh cadence. Long enough that a missed
// beat or a slow cycle never flaps health, short enough that a feed
// which has genuinely stopped is caught within minutes rather than days.
export const LIVE_POSITIONS_STALE_AFTER_MS = 10 * 60_000;

const DIR = v => {
  const d = String(v || '').trim().toUpperCase();
  if (['SELL', 'SHORT'].includes(d)) return 'SHORT';
  if (['BUY', 'LONG'].includes(d)) return 'LONG';
  return '';
};

const num = v => {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

export const positionEpicOf = p =>
  String(p?.market?.epic || p?.position?.epic || p?.epic || '').trim().toUpperCase();

const brokerFields = p => {
  const pos = p?.position || {};
  return {
    epic: positionEpicOf(p),
    direction: DIR(pos.direction ?? p.direction),
    size: num(pos.size ?? p.size),
    stop_level: num(pos.stopLevel ?? p.stopLevel),
    level: num(pos.level ?? p.level),
    opened_at: pos.createdDateUTC || pos.createdDate || null
  };
};

const bookFields = t => ({
  id: t?.id ?? null,
  epic: String(t?.ticker || '').trim().toUpperCase(),
  direction: DIR(t?.direction),
  size: num(t?.size ?? t?.data?.size),
  stop_level: num(t?.sl ?? t?.stop_loss),
  opened_at: t?.opened_at || null
});

// Match on epic + direction + size. NEVER on deal id: /confirms returns
// WORKING-ORDER ids and /positions returns POSITION ids, one hex digit
// apart, so a deal-id join silently mismatches.
const keyOf = r => `${r.epic}|${r.direction}|${r.size ?? 'NA'}`;

export function reconcilePositions({
  trades = [],
  livePositions = [],
  unmanagedEpics = new Set(),
  updatedAt = null,
  now = new Date(),
  staleAfterMs = LIVE_POSITIONS_STALE_AFTER_MS
} = {}) {
  const ts = updatedAt ? Date.parse(updatedAt) : NaN;
  const ageMs = Number.isFinite(ts) ? now.getTime() - ts : null;
  const ageMinutes = ageMs === null ? null : Math.round(ageMs / 60000);
  const stale = ageMs === null || ageMs > staleAfterMs;

  const books = trades
    .filter(t => ['OPEN', 'PARTIAL'].includes(t?.status))
    .map(bookFields);
  const broker = livePositions.map(brokerFields);

  // Every open broker position with no stop, regardless of tracking.
  // This single field would have surfaced RKLB on day one.
  const unstopped = broker
    .filter(b => b.stop_level === null)
    .map(b => ({ ...b, acknowledged_unmanaged: unmanagedEpics.has(b.epic) }));

  const out = {
    updated_at: updatedAt,
    age_minutes: ageMinutes,
    stale,
    stale_after_minutes: Math.round(staleAfterMs / 60000),
    comparable: !stale,
    broker_count: broker.length,
    book_count: books.length,
    matched: [],
    in_broker_not_in_books: [],
    in_books_not_in_broker: [],
    grace_pending: [],
    matched_but_differing: [],
    unstopped,
    issues: [],
    warnings: []
  };

  if (stale) {
    // Refuse to compare. Reporting a mismatch from a dead feed is worse
    // than reporting nothing, because it trains everyone to ignore it.
    out.warnings.push(`live_positions_stale:${ageMinutes === null ? 'never' : ageMinutes}`);
    if (unstopped.length) {
      out.warnings.push(`positions_without_stop:${unstopped.map(u => u.epic).join(',')}`);
    }
    return out;
  }

  const brokerBy = new Map(broker.map(b => [keyOf(b), b]));
  const bookBy = new Map(books.map(b => [keyOf(b), b]));

  // A trade opened AFTER this snapshot was taken cannot appear in it.
  // Treating that as a phantom fires a false alarm on every entry.
  // An unparseable opened_at is deliberately NOT granted grace: an
  // absent timestamp must fail loud, not buy silence.
  const snapshotMs = Number.isFinite(ts) ? ts : null;
  const openedAfterSnapshot = t => {
    if (snapshotMs === null || !t.opened_at) return false;
    const o = Date.parse(t.opened_at);
    return Number.isFinite(o) && o > snapshotMs;
  };

  for (const b of books) {
    const m = brokerBy.get(keyOf(b));
    if (m) out.matched.push({ epic: b.epic, direction: b.direction, size: b.size,
                              book_stop: b.stop_level, broker_stop: m.stop_level });
    else if (openedAfterSnapshot(b))
      out.grace_pending.push({ ...b, reason: 'opened after the live snapshot was taken' });
    else out.in_books_not_in_broker.push(b);
  }

  for (const b of broker) {
    if (bookBy.has(keyOf(b))) continue;
    const acknowledged = unmanagedEpics.has(b.epic);
    out.in_broker_not_in_books.push({
      ...b,
      acknowledged_unmanaged: acknowledged,
      unstopped: b.stop_level === null
    });
  }

  // Same epic+direction present on both sides but a different size is
  // drift, not a clean untracked/phantom pair.
  for (const b of out.in_broker_not_in_books.slice()) {
    const peer = out.in_books_not_in_broker.find(x => x.epic === b.epic && x.direction === b.direction);
    if (!peer) continue;
    out.matched_but_differing.push({ epic: b.epic, direction: b.direction,
                                     broker_size: b.size, book_size: peer.size });
    out.in_broker_not_in_books = out.in_broker_not_in_books.filter(x => x !== b);
    out.in_books_not_in_broker = out.in_books_not_in_broker.filter(x => x !== peer);
  }

  // Untracked positions we have NOT acknowledged are a real issue.
  const unacked = out.in_broker_not_in_books.filter(b => !b.acknowledged_unmanaged);
  if (unacked.length) out.issues.push('untracked_broker_positions');
  if (out.in_books_not_in_broker.length) out.issues.push('phantom_book_positions');
  if (out.matched_but_differing.length) out.issues.push('position_size_drift');

  const acked = out.in_broker_not_in_books.filter(b => b.acknowledged_unmanaged);
  if (acked.length) out.warnings.push(`acknowledged_unmanaged:${acked.map(b => b.epic).join(',')}`);
  if (unstopped.length) out.warnings.push(`positions_without_stop:${unstopped.map(u => u.epic).join(',')}`);

  return out;
}
