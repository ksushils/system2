// ════════════════════════════════════════════════════════════
// SHARED DEAL-ID RESOLVER
//
// Capital's /confirms/{dealReference} can return a WORKING-ORDER id.
// /positions returns the POSITION id. They are different objects, and
// storing one where the other belongs breaks every deal-id-keyed
// operation silently -- reconciliation survives on epic+direction+size,
// so nothing complains while closes quietly fail to match.
//
// Exactly one workflow ever got this right: indices' "Confirm & Register
// Deal" node, which polls /positions 3x at 3s and matches on
// epic + direction + size + open time. fmp, pa, comm and mean_reversion
// poll /confirms once with no retry and no re-resolution; volume has no
// resolution step at all and stores the order response's id. The
// knowledge existed in one place and was never propagated -- the same
// shape as capitalLogin's unused cache and the pnl_gross columns the
// bulk INSERT omitted.
//
// This is that node's logic, ported verbatim, so the five broken
// scanners need a one-node change each instead of five bespoke ports.
//
// NEVER derive an id arithmetically. The "+1 hex digit" pattern is an
// artefact of this account's id allocation, not a rule: MCD's
// working-order id and ADBE's POSITION id share a final segment despite
// being different deals.
// ════════════════════════════════════════════════════════════

// Ported from indices verbatim. The 6s span is deliberate -- Capital
// needs seconds, not tens of seconds, and a longer poll would block the
// caller's order path for no gain.
export const RESOLVE_ATTEMPTS = 3;
export const RESOLVE_BACKOFF_MS = 3000;
export const OPEN_TIME_TOLERANCE_MS = 60_000;
export const SIZE_TOLERANCE = 0.000001;
export const POSITIONS_TIMEOUT_MS = 8000;

export const RESOLVE_FAILED = 'POSITION_ID_RESOLUTION_FAILED';
// indices' existing string, reused rather than reworded so the two
// implementations stay greppable as one thing.
export const RESOLVE_FAILED_MESSAGE =
  'No Capital position matched epic+direction+size+open_time after 3 attempts';

// The one deliberate deviation from the verbatim port. indices compares
// raw uppercased strings because it only ever talks to itself. This
// endpoint serves five scanners with three vocabularies -- Capital says
// BUY/SELL, some payloads say LONG/SHORT -- so both sides are normalised.
// Without this, a caller sending LONG would silently never match.
const DIR = v => {
  const d = String(v || '').trim().toUpperCase();
  if (['SELL', 'SHORT'].includes(d)) return 'SHORT';
  if (['BUY', 'LONG'].includes(d)) return 'LONG';
  return '';
};

export function matchesPosition(p, { epic, direction, size, orderTime }) {
  const pEpic = String(p?.market?.epic || p?.position?.epic || '').toUpperCase();
  const pDir = DIR(p?.position?.direction);
  const pSize = Number(p?.position?.size);
  const opened = Date.parse(p?.position?.createdDateUTC || p?.position?.createdDate || '');
  const timeMatches = Number.isFinite(opened) && Math.abs(opened - orderTime) <= OPEN_TIME_TOLERANCE_MS;
  const sizeMatches = Number.isFinite(pSize) && Number.isFinite(size)
    && Math.abs(pSize - size) < SIZE_TOLERANCE;
  return pEpic === epic && pDir === direction && sizeMatches && timeMatches;
}

// Injectable so the poll can be exercised without reaching Capital.
const realSleep = ms => new Promise(r => setTimeout(r, ms));

export async function resolvePositionDealId(
  { epic, direction, size, opened_at },
  { login, invalidate, fetchImpl = fetch, sleep = realSleep } = {}
) {
  const wantEpic = String(epic || '').trim().toUpperCase();
  const wantDir = DIR(direction);
  const wantSize = Number(size);
  const orderTime = Date.parse(opened_at || new Date().toISOString());

  if (!wantEpic) return { ok: false, error: 'epic required' };
  if (!wantDir) return { ok: false, error: 'direction must be BUY, SELL, LONG or SHORT' };
  if (!Number.isFinite(wantSize) || wantSize <= 0) return { ok: false, error: 'size must be a positive number' };
  if (!Number.isFinite(orderTime)) return { ok: false, error: 'opened_at must be a parseable timestamp' };

  let attempts = 0;
  let resolved = null;
  let lastStatus = null;

  for (let attempt = 1; attempt <= RESOLVE_ATTEMPTS && !resolved; attempt++) {
    attempts = attempt;
    try {
      const session = await login();
      if (!session?.ok) { lastStatus = session?.status ?? null; }
      else {
        const r = await fetchImpl(`${session.baseUrl}/api/v1/positions`, {
          headers: {
            'X-CAP-API-KEY': session.apiKey,
            CST: session.cst,
            'X-SECURITY-TOKEN': session.token,
            Accept: 'application/json'
          },
          signal: AbortSignal.timeout(POSITIONS_TIMEOUT_MS)
        });
        lastStatus = r.status;
        // An expired CST must drop the shared cache, or every remaining
        // attempt reuses the same dead session.
        if (r.status === 401) invalidate?.();
        else if (r.ok) {
          const body = await r.json();
          resolved = (body?.positions || []).find(p =>
            matchesPosition(p, { epic: wantEpic, direction: wantDir, size: wantSize, orderTime })) || null;
        }
      }
    } catch { /* indices swallows here too: a failed attempt is just an attempt */ }
    if (!resolved && attempt < RESOLVE_ATTEMPTS) await sleep(RESOLVE_BACKOFF_MS);
  }

  if (!resolved?.position?.dealId) {
    return {
      ok: false, reason: RESOLVE_FAILED, error: RESOLVE_FAILED_MESSAGE,
      attempts, last_http_status: lastStatus,
      queried: { epic: wantEpic, direction: wantDir, size: wantSize, opened_at }
    };
  }

  return {
    ok: true,
    deal_id: resolved.position.dealId,
    attempts,
    resolved_at: new Date().toISOString(),
    matched: {
      epic: wantEpic, direction: wantDir, size: Number(resolved.position.size),
      level: resolved.position.level ?? null,
      opened_at: resolved.position.createdDateUTC || resolved.position.createdDate || null
    }
  };
}

export const AMBIGUOUS = 'POSITION_ID_AMBIGUOUS';
export const CONFIRM_FAILED = 'DEAL_REFERENCE_UNRESOLVED';
export const VALIDATION_FAILED = 'POSITION_VALIDATION_FAILED';

// Resolve ONE leg by its own dealReference.
//
// /confirms/{ref} yields the affected deal id. That id is then located
// in /positions -- but the id /confirms returns can be a WORKING-ORDER
// id, so the match rule is asserted from live data rather than assumed:
// a position qualifies if its dealId equals the confirmed id, OR if its
// own dealReference is 'p_' + that id. Which rule fired is reported back
// as match_rule so the assumption stays visible instead of buried.
export async function resolveLegByReference(
  dealReference,
  { epic, direction, size },
  { login, invalidate, fetchImpl = fetch, sleep = realSleep } = {}
) {
  const ref = String(dealReference || '').trim();
  if (!ref) return { ok: false, reason: 'BAD_REQUEST', error: 'deal_reference required' };

  const wantEpic = String(epic || '').trim().toUpperCase();
  const wantDir = DIR(direction);
  const wantSize = Number(size);

  let confirmedId = null, attempts = 0, lastStatus = null, matchRule = null, matched = null;

  for (let attempt = 1; attempt <= RESOLVE_ATTEMPTS && !matched; attempt++) {
    attempts = attempt;
    try {
      const session = await login();
      if (!session?.ok) { lastStatus = session?.status ?? null; }
      else {
        const H = {
          'X-CAP-API-KEY': session.apiKey, CST: session.cst,
          'X-SECURITY-TOKEN': session.token, Accept: 'application/json'
        };
        if (!confirmedId) {
          const c = await fetchImpl(
            `${session.baseUrl}/api/v1/confirms/${encodeURIComponent(ref)}`,
            { headers: H, signal: AbortSignal.timeout(POSITIONS_TIMEOUT_MS) });
          lastStatus = c.status;
          if (c.status === 401) invalidate?.();
          else if (c.ok) {
            const cb = await c.json();
            confirmedId = cb?.dealId || cb?.affectedDeals?.[0]?.dealId || null;
          }
        }
        if (confirmedId) {
          const r = await fetchImpl(`${session.baseUrl}/api/v1/positions`,
            { headers: H, signal: AbortSignal.timeout(POSITIONS_TIMEOUT_MS) });
          lastStatus = r.status;
          if (r.status === 401) invalidate?.();
          else if (r.ok) {
            const body = await r.json();
            for (const p of (body?.positions || [])) {
              const pid = p?.position?.dealId;
              const pref = String(p?.position?.dealReference || '');
              if (pid && pid === confirmedId) { matched = p; matchRule = 'dealId'; break; }
              if (pid && pref && pref === 'p_' + confirmedId) { matched = p; matchRule = 'p_prefix'; break; }
            }
          }
        }
      }
    } catch { /* an attempt that throws is just an attempt, as above */ }
    if (!matched && attempt < RESOLVE_ATTEMPTS) await sleep(RESOLVE_BACKOFF_MS);
  }

  if (!confirmedId) {
    return { ok: false, reason: CONFIRM_FAILED, deal_reference: ref, attempts,
             last_http_status: lastStatus,
             error: `/confirms returned no dealId for reference ${ref}` };
  }
  if (!matched?.position?.dealId) {
    return { ok: false, reason: RESOLVE_FAILED, deal_reference: ref, confirmed_id: confirmedId,
             attempts, last_http_status: lastStatus,
             error: `no live position matched confirmed id ${confirmedId} by dealId or p_ prefix` };
  }

  // VALIDATE before returning. Identification came from the reference;
  // this only confirms the position is the one the caller described.
  const gotEpic = String(matched?.market?.epic || matched?.position?.epic || '').toUpperCase();
  const gotDir = DIR(matched?.position?.direction);
  const gotSize = Number(matched?.position?.size);
  const bad = [];
  if (wantEpic && gotEpic !== wantEpic) bad.push(`epic ${gotEpic} != ${wantEpic}`);
  if (wantDir && gotDir !== wantDir) bad.push(`direction ${gotDir} != ${wantDir}`);
  if (Number.isFinite(wantSize) && Math.abs(gotSize - wantSize) >= SIZE_TOLERANCE)
    bad.push(`size ${gotSize} != ${wantSize}`);
  if (bad.length) {
    return { ok: false, reason: VALIDATION_FAILED, deal_reference: ref,
             deal_id: matched.position.dealId, attempts, mismatches: bad,
             error: `position ${matched.position.dealId} does not match the described leg: ${bad.join('; ')}` };
  }

  return {
    ok: true, deal_reference: ref, deal_id: matched.position.dealId,
    confirmed_id: confirmedId, match_rule: matchRule, attempts,
    resolved_at: new Date().toISOString(),
    matched: { epic: gotEpic, direction: gotDir, size: gotSize,
               level: matched.position.level ?? null,
               opened_at: matched.position.createdDateUTC || matched.position.createdDate || null }
  };
}

// Resolve every supplied leg, then refuse the whole set if any two land
// on the same position id. Failing BOTH is the point: booking one leg
// against the other's position must be impossible by construction, and a
// partial success would leave exactly that.
export async function resolveLegs(references, describe, deps) {
  const refs = (Array.isArray(references) ? references : [references])
    .map(r => String(r || '').trim()).filter(Boolean);
  const legs = [];
  for (const r of refs) legs.push(await resolveLegByReference(r, describe, deps));

  const byId = new Map();
  for (const l of legs) {
    if (!l.ok) continue;
    if (!byId.has(l.deal_id)) byId.set(l.deal_id, []);
    byId.get(l.deal_id).push(l.deal_reference);
  }
  const collisions = [...byId.entries()].filter(([, rs]) => rs.length > 1);
  if (collisions.length) {
    return {
      ok: false, reason: AMBIGUOUS,
      error: 'two or more legs resolved to the same position id; all legs failed',
      collisions: collisions.map(([id, rs]) => ({ deal_id: id, deal_references: rs })),
      legs
    };
  }
  return { ok: legs.every(l => l.ok), legs };
}

export function attachBrokerResolve(app, { scannerAuth, capitalLogin, invalidateCapitalSession, journalEvent }) {
  app.post('/api/broker/resolve-position', scannerAuth, async (req, res) => {
    const b = req.body || {};
    const scanner = String(b.scanner || '').trim().toLowerCase() || null;
    const started = Date.now();
    try {
      const deps = { login: capitalLogin, invalidate: invalidateCapitalSession };
      // Optional per-leg path. Absent deal_reference, behaviour is byte
      // for byte what it was: the single-leg epic+direction+size+time match.
      const refs = b.deal_reference ?? b.deal_references ?? null;
      const out = refs
        ? await resolveLegs(refs, { epic: b.epic, direction: b.direction, size: b.size }, deps)
        : await resolvePositionDealId(b, deps);

      if (!out.ok) {
        // Same journal the /api/scanner/error receiver writes, so a
        // resolution failure shows up in scanner_errors_24h and the daily
        // digest alongside every other scanner failure.
        await journalEvent?.('scanner_error', {
          scanner, ticker: String(b.epic || '').toUpperCase() || null,
          payload: {
            scanner, stage: 'resolve_position', error_class: 'OTHER',
            reason: out.reason || 'BAD_REQUEST', message: out.error,
            attempts: out.attempts ?? 0, http_status: out.last_http_status ?? null,
            duration_ms: Date.now() - started, occurred_at: new Date().toISOString()
          }
        }).catch(() => {});
        const st = out.reason === AMBIGUOUS ? 409
          : (out.reason === RESOLVE_FAILED || out.reason === CONFIRM_FAILED) ? 404 : 400;
        return res.status(st).json(out);
      }
      res.json(out);
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  });
}
