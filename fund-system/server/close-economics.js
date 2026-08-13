// ════════════════════════════════════════════════════════════
// CLOSE ECONOMICS RESOLVER
//
// A close that records no price and no P&L is worse than a failed
// close: the position leaves the book, the money is real, and nothing
// downstream can ever reconstruct it. Trades 135/140/142 sat that way
// for days, and two of them turned out to be winners whose absence kept
// indices behind its own consecutive-loss breaker.
//
// /history/transactions is empty on this account whatever `type` is
// passed and is NOT the source. The source is:
//
//   GET /api/v1/history/activity?from=<past>&to=<past>&detailed=true
//
// Two traps, both of which look exactly like "no data":
//   * `to` in the FUTURE (server-local) rejects the whole query with
//     error.invalid.daterange. Windows are therefore clamped to now.
//   * detailed=true is MANDATORY; without it the `details` object --
//     level, openPrice, size, direction -- is absent entirely.
//
// A close is a POSITION/ACCEPTED whose source is SL/TP/SYSTEM and whose
// details.direction is OPPOSITE the position. details.level is the
// close price, details.openPrice the entry. dateUTC is returned
// alongside server-local date, so no offset has to be derived.
// ════════════════════════════════════════════════════════════

export const CLOSE_SOURCES = new Set(['SL', 'TP', 'SYSTEM', 'USER']);
export const RESOLVE_FAILED = 'CLOSE_ECONOMICS_UNRESOLVED';
export const ACTIVITY_TIMEOUT_MS = 10_000;

// /history/activity rejects a span much beyond a day with
// error.invalid.daterange -- the SAME error a future `to` produces, which
// is why the two were easy to confuse. A single opened_at..now request
// therefore fails for any trade older than about a day, so the search
// pages backwards in safe windows instead.
export const WINDOW_MS = 23 * 60 * 60_000;
export const MAX_WINDOWS = 8;   // ~7.7 days of history

const DIR = v => {
  const d = String(v || '').trim().toUpperCase();
  if (['SELL', 'SHORT'].includes(d)) return 'SHORT';
  if (['BUY', 'LONG'].includes(d)) return 'LONG';
  return '';
};
const opposite = d => (d === 'LONG' ? 'SHORT' : d === 'SHORT' ? 'LONG' : '');

// Capital wants YYYY-MM-DDTHH:MM:SS. A trailing Z or a bare date give
// error.invalid.from.
export const capStamp = d => new Date(d).toISOString().slice(0, 19);

// Exported for testing without reaching Capital.
export function pickClose(activities, { deal_id, epic, direction, size }) {
  const want = DIR(direction);
  const wantEpic = String(epic || '').trim().toUpperCase();
  const wantSize = Number(size);
  const closing = opposite(want);

  const candidates = (activities || []).filter(a => {
    const d = a?.details || {};
    if (d.level === null || d.level === undefined || Number(d.level) === 0) return false;
    if (!CLOSE_SOURCES.has(String(a.source || '').toUpperCase())) return false;
    // The close leg trades the OPPOSITE way to the position it closes.
    return DIR(d.direction) === closing;
  });

  // deal_id first -- it is exact. The rest is corroboration, not search.
  let hit = candidates.find(a => a.dealId === deal_id);
  let matchedBy = 'deal_id';
  if (!hit) {
    hit = candidates.find(a => {
      const d = a.details || {};
      return String(a.epic || '').toUpperCase() === wantEpic
        && Number.isFinite(Number(d.size)) && Number.isFinite(wantSize)
        && Math.abs(Number(d.size) - wantSize) < 1e-6;
    });
    matchedBy = 'epic+size+opposite_direction';
  }
  if (!hit) return { ok: false, reason: RESOLVE_FAILED, error: 'no closing activity matched', examined: candidates.length };

  const d = hit.details || {};
  return {
    ok: true,
    close_price: Number(d.level),
    close_at: hit.dateUTC ? `${hit.dateUTC}Z`.replace(/ZZ$/, 'Z') : null,
    close_source: String(hit.source || '').toUpperCase(),
    open_price: d.openPrice ?? null,
    size: d.size ?? null,
    deal_id: hit.dealId ?? null,
    matched_by: matchedBy,
    examined: candidates.length
  };
}

export async function resolveCloseEconomics(
  { deal_id, epic, direction, size, opened_at },
  { login, invalidate, fetchImpl = fetch, now = () => Date.now() } = {}
) {
  if (!deal_id && !epic) return { ok: false, error: 'deal_id or epic required' };

  const session = await login();
  if (!session?.ok) {
    return { ok: false, reason: RESOLVE_FAILED, error: `capital login failed ${session?.status ?? ''}`.trim() };
  }

  // `to` must never run past now (server-local), and each window must stay
  // inside the span limit. Page backwards from now until the close is
  // found, the trade's own open is passed, or MAX_WINDOWS is exhausted.
  const openedMs = Date.parse(opened_at || '');
  const stopAt = Number.isFinite(openedMs) ? openedMs - 60_000 : -Infinity;
  let to = new Date(now());
  const windows = [];

  for (let i = 0; i < MAX_WINDOWS; i++) {
    const from = new Date(Math.max(to.getTime() - WINDOW_MS, stopAt));
    if (from.getTime() >= to.getTime()) break;
    const w = { from: capStamp(from), to: capStamp(to) };
    windows.push(w);

    const url = `${session.baseUrl}/api/v1/history/activity`
      + `?from=${w.from}&to=${w.to}&detailed=true`;

    let r;
    try {
      r = await fetchImpl(url, {
        headers: {
          'X-CAP-API-KEY': session.apiKey, CST: session.cst,
          'X-SECURITY-TOKEN': session.token, Accept: 'application/json'
        },
        signal: AbortSignal.timeout(ACTIVITY_TIMEOUT_MS)
      });
    } catch (e) {
      return { ok: false, reason: RESOLVE_FAILED, error: `activity request failed: ${e.message}`, windows };
    }
    if (r.status === 401) { invalidate?.(); return { ok: false, reason: RESOLVE_FAILED, error: 'capital 401 — session invalidated, retry', windows }; }
    if (!r.ok) {
      const body = await r.text().catch(() => '');
      return { ok: false, reason: RESOLVE_FAILED, error: `activity http ${r.status}`, detail: body.slice(0, 160), windows };
    }

    const body = await r.json().catch(() => null);
    const out = pickClose(body?.activities, { deal_id, epic, direction, size });
    if (out.ok) return { ...out, windows_searched: windows.length, window: w };

    if (from.getTime() <= stopAt) break;   // walked back past the open
    to = from;
  }

  return { ok: false, reason: RESOLVE_FAILED,
           error: 'no closing activity matched in any searched window',
           windows_searched: windows.length, windows };
}
