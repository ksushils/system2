// ════════════════════════════════════════════════════════════
// fleet_expected — what we believe SHOULD be running.
//
// Seeded only from records that actually exist:
//   * today's switchover records (comm, volume)
//   * /root/test-history/workflow-versions.json, but ONLY where its
//     entry is for the workflow that is still the active one
//
// A guessed hash is worse than an admitted gap: it would read as
// verified when nothing verified it.
//
// expected_hash stays UNKNOWN deliberately, and is not a gap. n8n mints
// a fresh versionId on every save -- switchover.js does exactly that,
// and crypto's draft-vs-active versionId split shows n8n versioning each
// edit -- so expected_version_id ALREADY detects a content change. A
// separate payload hash would add no detection while adding a
// false-alarm surface: n8n re-serialises node JSON on a UI save, so the
// hash can move with no logic change at all. Storing one would produce
// alerts that mean nothing, which is worse than the honest UNKNOWN.
// ════════════════════════════════════════════════════════════
import pg from 'pg';

const { Pool } = pg;
const DATABASE_URL = process.env.DATABASE_URL || '';
const pool = DATABASE_URL
  ? new Pool({ connectionString: DATABASE_URL, max: Number(process.env.FLEET_POOL_MAX || 2), application_name: 'fund-system-fleet' })
  : null;

let initDone = false;

// provenance is stored so nobody has to re-derive where a value came from
export const FLEET_SEED = [
  { scanner: 'crypto',          should_be_active: true,  expected_version_id: '23eb3c78-8d56-49b3-a3dc-ad4c51856615', schedule: '24/7',            source: 'workflow-versions.json 2026-08-08' },
  { scanner: 'comm',            should_be_active: true,  expected_version_id: 'f893bd36-4906-442f-b88a-1b4b0df2edc4', schedule: 'weekday+sunday',  source: 'switchover 2026-08-09' },
  { scanner: 'volume',          should_be_active: true,  expected_version_id: '7de57aa9-7033-417a-ba0f-3bd8bee1a56a', schedule: 'weekday-only',    source: 'switchover 2026-08-09' },
  // Baselined 2026-08-09 by declaring verified current state correct.
  // Each passed four checks first: exactly one active workflow for the
  // self-declared tag, activeVersionId non-null, no other row for that
  // tag holding a live activeVersionId, and the workflow's own SCANNER
  // constant matching the tag.
  { scanner: 'indices',         should_be_active: true,  expected_version_id: '92181f45-9b19-416c-9b42-d5024ea105fa', schedule: 'weekday-only', source: 'verified-current 2026-08-09' },
  { scanner: 'mean_reversion',  should_be_active: true,  expected_version_id: '446b6c50-0f6e-44bb-9d64-eea0f94f8c88', schedule: '24/7',         source: 'verified-current 2026-08-09' },
  { scanner: 'forex',           should_be_active: true,  expected_version_id: 'eb92c140-dcb7-495c-a0c5-d548ec544d70', schedule: 'weekday-only', source: 'verified-current 2026-08-09' },
  { scanner: 'pa',              should_be_active: true,  expected_version_id: '7ad64106-1788-4a13-bb1f-97d2da6e859d', schedule: 'weekday-only', source: 'verified-current 2026-08-09' },
  { scanner: 'fmp',             should_be_active: true,  expected_version_id: '4280b773-2362-4224-ba68-dc7ba4d2101c', schedule: 'weekday-only', source: 'verified-current 2026-08-09' },
  { scanner: 'fmp_alpaca',      should_be_active: true,  expected_version_id: '02a65aea-4116-442b-bbcc-b16dc2adc72f', schedule: 'weekday-only', source: 'verified-current 2026-08-09' },
  { scanner: 'failed_breakout', should_be_active: false, expected_version_id: null, schedule: null,           source: 'deliberately inactive' }
];

export async function initFleetStore() {
  if (!pool) throw new Error('Postgres is required for fleet_expected');
  if (initDone) return;
  await pool.query(`
    CREATE TABLE IF NOT EXISTS fleet_expected (
      scanner             text PRIMARY KEY,
      should_be_active    boolean,
      expected_version_id text,
      expected_hash       text,
      schedule            text,
      source              text,
      updated_at          timestamptz DEFAULT now()
    );
  `);
  // additive seed: never overwrites a row an operator has since corrected
  for (const r of FLEET_SEED) {
    await pool.query(
      `INSERT INTO fleet_expected (scanner, should_be_active, expected_version_id, expected_hash, schedule, source)
       VALUES ($1,$2,$3,NULL,$4,$5) ON CONFLICT (scanner) DO NOTHING`,
      [r.scanner, r.should_be_active, r.expected_version_id, r.schedule, r.source]);
  }
  initDone = true;
}

export async function getFleetExpected() {
  await initFleetStore();
  const { rows } = await pool.query('SELECT * FROM fleet_expected');
  return Object.fromEntries(rows.map(r => [r.scanner, r]));
}

// Used by the verification harness to simulate a mismatch in OUR table
// only. n8n is never written to.
export async function setFleetExpectedVersion(scanner, versionId) {
  await initFleetStore();
  const { rows } = await pool.query(
    'UPDATE fleet_expected SET expected_version_id=$2, updated_at=now() WHERE scanner=$1 RETURNING *',
    [scanner, versionId]);
  return rows[0] || null;
}
