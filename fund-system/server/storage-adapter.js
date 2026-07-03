// ════════════════════════════════════════════════════════════
// Storage Adapter — lets the existing server use Postgres
// WITHOUT rewriting all the db.data.x code.
//
// Strategy: keep the in-memory db.data object the server already uses,
// but (a) LOAD it from Postgres on boot, and (b) on every save(), persist
// changed collections back to Postgres. Optionally DUAL_WRITE to fund.json
// too as a safety net during the transition.
//
// This gives you Postgres durability + concurrent-safe writes on the money
// tables, while the rest of the server code stays untouched.
// ════════════════════════════════════════════════════════════

const USE_PG     = process.env.USE_POSTGRES === 'true';
const DUAL_WRITE = process.env.DUAL_WRITE === 'true';
const PG_URL     = process.env.DATABASE_URL;

let pool = null;
// Lazy-load pg ONLY when Postgres is enabled, so the server runs fine on
// fund.json without the pg package installed.
async function getPool() {
  if (!USE_PG) return null;
  if (pool) return pool;
  const pg = (await import('pg')).default;
  pool = new pg.Pool({ connectionString: PG_URL, max: 10 });
  return pool;
}

// Load the entire dataset from Postgres into the in-memory shape the server expects
export async function loadFromPostgres() {
  if (!USE_PG) return null;
  const pool = await getPool();
  const data = {};
  const one = async (t) => (await pool.query(`SELECT data FROM ${t} WHERE id=1`)).rows[0]?.data;
  const many = async (t) => (await pool.query(`SELECT data FROM ${t} ORDER BY created_at`)).rows.map(r=>r.data);

  data.fund            = await one('fund_config') || {};
  data.scanner_config  = await one('scanner_config') || {};
  data.investors       = (await pool.query('SELECT data FROM investors')).rows.map(r=>r.data);
  data.stakes          = await many('stakes');
  data.allocations     = await many('allocations');
  data.signals         = await many('signals');
  data.rejections      = await many('rejections');
  data.updates         = await many('updates');
  data.pings           = await many('pings');
  data.withdrawals     = await many('withdrawals');
  data.fees            = await many('fees');
  data.monthly_snapshots = await many('monthly_snapshots');
  data.sessions        = (await pool.query('SELECT data FROM sessions')).rows.map(r=>r.data);
  data.trades          = (await pool.query('SELECT data FROM trades ORDER BY created_at')).rows.map(r=>r.data);
  data.risk_ledger     = (await pool.query('SELECT scanner,ticker,deal_id,risk_amount,opened_at FROM risk_ledger')).rows;
  data.trade_brain     = (await pool.query('SELECT * FROM trade_brain')).rows.map(r=>({
    id:r.id, scanner:r.scanner, setup_type:r.setup_type, direction:r.direction, ticker:r.ticker,
    deal_id:r.deal_id, features:r.features, outcome:{win:r.win,r_multiple:r.r_multiple,pnl:r.pnl}, recorded_at:r.recorded_at
  }));
  // risk_settings as array (matches server expectations)
  const rs = (await pool.query('SELECT investor_id,data FROM risk_settings')).rows;
  data.risk_settings = rs.map(r => ({ ...r.data, investor_id: r.investor_id }));
  // heartbeats keyed by scanner
  const hb = (await pool.query('SELECT scanner,ts,status,msg FROM heartbeats')).rows;
  data.heartbeats = {}; hb.forEach(h=>data.heartbeats[h.scanner]={ts:h.ts,status:h.status,msg:h.msg});

  return data;
}

// Persist the in-memory data back to Postgres. Called from save().
// Uses upserts so it's idempotent and safe under concurrency.
export async function saveToPostgres(data) {
  if (!USE_PG) return;
  const pool = await getPool();
  const c = await pool.connect();
  try {
    await c.query('BEGIN');
    await c.query(`INSERT INTO fund_config(id,data) VALUES(1,$1) ON CONFLICT(id) DO UPDATE SET data=$1,updated_at=now()`, [data.fund||{}]);
    await c.query(`INSERT INTO scanner_config(id,data) VALUES(1,$1) ON CONFLICT(id) DO UPDATE SET data=$1,updated_at=now()`, [data.scanner_config||{}]);

    // Investors
    for (const i of data.investors||[])
      await c.query(`INSERT INTO investors(id,name,email,pin,active,data) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(id) DO UPDATE SET name=$2,email=$3,pin=$4,active=$5,data=$6`,
        [i.id,i.name,i.email||null,i.pin||null,i.active!==false,i]);

    // Trades — hot columns + payload
    for (const t of data.trades||[])
      await c.query(`INSERT INTO trades(id,scanner,ticker,deal_id,direction,setup_type,status,entry,sl,tp1,tp2,close_price,pnl,risk_amount,quality_score,rsi,volume_ratio,htf_bias,spy_regime,vix_level,gap_pct,data,opened_at,closed_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24)
        ON CONFLICT(id) DO UPDATE SET status=$7,close_price=$12,pnl=$13,closed_at=$24,data=$22`,
        [t.id,t.scanner,t.ticker,t.deal_id||null,t.direction||null,t.setup_type||t.trade_type||null,t.status||null,
         t.entry??t.entry_price??null,t.sl??t.stop_loss??null,t.tp1??t.take_profit_1??null,t.tp2??t.take_profit_2??null,
         t.close_price??null,t.pnl??null,t.risk_amount??null,t.quality_score??t.signal_score??null,t.rsi??null,
         t.volume_ratio??null,t.htf_bias||null,t.spy_regime||null,t.vix_level??null,t.gap_pct??null,t,t.opened_at||t.ts||null,t.closed_at||null]);

    // Brain
    for (const b of data.trade_brain||[])
      await c.query(`INSERT INTO trade_brain(id,scanner,setup_type,direction,ticker,deal_id,features,win,r_multiple,pnl,recorded_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT(id) DO NOTHING`,
        [b.id,b.scanner,b.setup_type,b.direction,b.ticker,b.deal_id||null,b.features,b.outcome?.win,b.outcome?.r_multiple,b.outcome?.pnl,b.recorded_at]);

    // Risk ledger — replace whole set (it's small + changes atomically)
    await c.query('DELETE FROM risk_ledger');
    for (const r of data.risk_ledger||[])
      await c.query(`INSERT INTO risk_ledger(deal_id,scanner,ticker,risk_amount,opened_at) VALUES($1,$2,$3,$4,$5) ON CONFLICT(deal_id) DO NOTHING`,
        [r.deal_id,r.scanner,r.ticker,r.risk_amount,r.opened_at||new Date()]);

    // Heartbeats
    for (const [s,h] of Object.entries(data.heartbeats||{}))
      await c.query(`INSERT INTO heartbeats(scanner,ts,status,msg) VALUES($1,$2,$3,$4) ON CONFLICT(scanner) DO UPDATE SET ts=$2,status=$3,msg=$4,updated_at=now()`,
        [s,h.ts,h.status,h.msg]);

    // Append-only logs (signals/rejections/updates/pings) + stakes/allocations/withdrawals
    const appendOnly = async (table, rows, cols) => {
      for (const r of rows||[]) {
        const vals = cols.map(col=>col==='data'?r:(r[col]??null));
        const ph = cols.map((_,i)=>`$${i+1}`).join(',');
        await c.query(`INSERT INTO ${table}(${cols.join(',')}) VALUES(${ph}) ON CONFLICT(${cols[0]}) DO NOTHING`, vals);
      }
    };
    await appendOnly('signals',    data.signals,    ['id','scanner','ticker','data']);
    await appendOnly('rejections', data.rejections, ['id','scanner','ticker','data']);
    await appendOnly('updates',    data.updates,    ['id','scanner','ticker','deal_id','data']);
    await appendOnly('pings',      data.pings,      ['id','scanner','data']);
    await appendOnly('stakes',     data.stakes,     ['id','investor_id','amount','type','data']);
    await appendOnly('allocations',data.allocations,['id','investor_id','data']);
    await appendOnly('withdrawals',data.withdrawals,['id','investor_id','amount','status','data']);

    // risk_settings (handles both array and object formats)
    const rsArray = Array.isArray(data.risk_settings) ? data.risk_settings : Object.entries(data.risk_settings || {}).map(([iid, rsv]) => ({ ...rsv, investor_id: iid }));
    for (const rsv of rsArray) {
      const iid = rsv.investor_id;
      if (!iid) continue;
      await c.query(`INSERT INTO risk_settings(investor_id,data) VALUES($1,$2) ON CONFLICT(investor_id) DO UPDATE SET data=$2`, [iid, rsv]);
    }

    await c.query('COMMIT');
  } catch(e) {
    await c.query('ROLLBACK');
    console.error('Postgres save failed (data still in memory + fund.json):', e.message);
  } finally {
    c.release();
  }
}

export const dualWriteEnabled = DUAL_WRITE;
export const postgresEnabled  = USE_PG;
