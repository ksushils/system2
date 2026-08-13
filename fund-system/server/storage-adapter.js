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
  await pool.query('ALTER TABLE risk_ledger ADD COLUMN IF NOT EXISTS direction TEXT');
  await pool.query('ALTER TABLE risk_ledger ADD COLUMN IF NOT EXISTS asset_class TEXT');
  await pool.query("ALTER TABLE risk_ledger ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'OPEN'");
  await pool.query('ALTER TABLE risk_ledger ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ');
  await pool.query('ALTER TABLE risk_ledger ADD COLUMN IF NOT EXISTS reservation_id TEXT');
  await pool.query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS spread_cost NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS commission NUMERIC DEFAULT 0;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS financing_accrued NUMERIC DEFAULT 0;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS pnl_gross NUMERIC;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS pnl_net NUMERIC;
    -- A column this SELECT reads is a column this function must create. engine_branch
    -- is created by initLayer1, which runs AFTER this load, so on the first boot
    -- after it was added the merge below threw and the whole load fell back to JSON.
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS engine_branch TEXT;`);
  await pool.query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS signal_to_order_ms BIGINT;
    ALTER TABLE trades ADD COLUMN IF NOT EXISTS order_to_fill_ms BIGINT;`);
  await pool.query("DELETE FROM rejections WHERE created_at < now() - interval '180 days'");
  await pool.query(`DELETE FROM rejections WHERE id IN (SELECT id FROM rejections ORDER BY created_at DESC OFFSET 100000)`);
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
  data.trades          = (await pool.query(`SELECT coalesce(data,'{}'::jsonb)||jsonb_build_object('spread_cost',spread_cost,'commission',commission,'financing_accrued',financing_accrued,'pnl_gross',pnl_gross,'pnl_net',pnl_net,'signal_to_order_ms',signal_to_order_ms,'order_to_fill_ms',order_to_fill_ms,'engine_branch',engine_branch) data FROM trades ORDER BY created_at`)).rows.map(r=>r.data);
  data.risk_ledger     = (await pool.query('SELECT scanner,ticker,deal_id,risk_amount,opened_at,direction,asset_class,status,expires_at,reservation_id FROM risk_ledger')).rows;
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

    // Sessions are a bounded cache, not an append-only log. Replace the table
    // so expired/pruned sessions do not accumulate forever in Postgres.
    await c.query('DELETE FROM sessions');
    for (const s of data.sessions||[])
      await c.query(`INSERT INTO sessions(token,role,investor_id,data) VALUES($1,$2,$3,$4)`,
        [s.token,s.is_admin?'admin':'investor',s.investor_id||null,s]);

    // Investors
    for (const i of data.investors||[])
      await c.query(`INSERT INTO investors(id,name,email,pin,active,data) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(id) DO UPDATE SET name=$2,email=$3,pin=$4,active=$5,data=$6`,
        [i.id,i.name,i.email||null,i.pin||null,i.active!==false,i]);

    // Trades — hot columns + payload
    for (const t of data.trades||[])
      await c.query(`INSERT INTO trades(id,scanner,ticker,deal_id,direction,setup_type,status,entry,sl,tp1,tp2,close_price,pnl,risk_amount,quality_score,rsi,volume_ratio,htf_bias,spy_regime,vix_level,gap_pct,data,opened_at,closed_at,engine_branch)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25)
        ON CONFLICT(id) DO UPDATE SET status=$7,close_price=$12,pnl=$13,closed_at=$24,data=$22,engine_branch=$25`,
        [t.id,t.scanner,t.ticker,t.deal_id||null,t.direction||null,t.setup_type||t.trade_type||null,t.status||null,
         t.entry??t.entry_price??null,t.sl??t.stop_loss??null,t.tp1??t.take_profit_1??null,t.tp2??t.take_profit_2??null,
         t.close_price??null,t.pnl??null,t.risk_amount??null,t.quality_score??t.signal_score??null,t.rsi??null,
         t.volume_ratio??null,t.htf_bias||null,t.spy_regime||null,t.vix_level??null,t.gap_pct??null,t,t.opened_at||t.ts||null,t.closed_at||null,t.engine_branch||null]);

    // Brain
    for (const b of data.trade_brain||[])
      await c.query(`INSERT INTO trade_brain(id,scanner,setup_type,direction,ticker,deal_id,features,win,r_multiple,pnl,recorded_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT(id) DO NOTHING`,
        [b.id,b.scanner,b.setup_type,b.direction,b.ticker,b.deal_id||null,b.features,b.outcome?.win,b.outcome?.r_multiple,b.outcome?.pnl,b.recorded_at]);

    // Risk ledger — replace whole set (it's small + changes atomically)
    await c.query('DELETE FROM risk_ledger');
    for (const r of data.risk_ledger||[])
      await c.query(`INSERT INTO risk_ledger(deal_id,scanner,ticker,risk_amount,opened_at,direction,asset_class,status,expires_at,reservation_id)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT(deal_id) DO UPDATE SET scanner=$2,ticker=$3,risk_amount=$4,opened_at=$5,direction=$6,asset_class=$7,status=$8,expires_at=$9,reservation_id=$10`,
        [r.deal_id,r.scanner,r.ticker,r.risk_amount,r.opened_at||new Date(),r.direction||null,r.asset_class||null,r.status||'OPEN',r.expires_at||null,r.reservation_id||null]);

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
    throw e;
  } finally {
    c.release();
  }
}

export const dualWriteEnabled = DUAL_WRITE;
export const postgresEnabled  = USE_PG;

export async function upsertHeartbeatPostgres(scanner, heartbeat = {}) {
  if (!USE_PG) return;
  const pool = await getPool();
  await pool.query(`
    INSERT INTO heartbeats(scanner,ts,status,msg)
    VALUES($1,$2,$3,$4)
    ON CONFLICT(scanner) DO UPDATE SET ts=$2,status=$3,msg=$4,updated_at=now()
  `, [scanner, heartbeat.ts, heartbeat.status, heartbeat.msg]);
}

export async function insertPingPostgres(ping = {}) {
  if (!USE_PG) return;
  const pool = await getPool();
  await pool.query(`
    INSERT INTO pings(id,scanner,data)
    VALUES($1,$2,$3)
    ON CONFLICT(id) DO NOTHING
  `, [String(ping.id), ping.scanner || null, ping]);
}

export async function insertSignalPostgres(signal = {}) {
  if (!USE_PG) return;
  const pool = await getPool();
  await pool.query(`
    INSERT INTO signals(id,scanner,ticker,data,config_hash)
    VALUES($1,$2,$3,$4,$5)
    ON CONFLICT(id) DO NOTHING
  `, [String(signal.id), signal.scanner || null, signal.ticker || null, signal, signal.config_hash || null]);
}

export async function insertRejectionPostgres(rejection = {}) {
  if (!USE_PG) return;
  const pool = await getPool();
  await pool.query(`
    INSERT INTO rejections(id,scanner,ticker,data)
    VALUES($1,$2,$3,$4)
    ON CONFLICT(id) DO NOTHING
  `, [String(rejection.id), rejection.scanner || null, rejection.ticker || null, rejection]);
}

export async function insertUpdatePostgres(update = {}) {
  if (!USE_PG) return;
  const pool = await getPool();
  await pool.query(`
    INSERT INTO updates(id,scanner,ticker,deal_id,data)
    VALUES($1,$2,$3,$4,$5)
    ON CONFLICT(id) DO NOTHING
  `, [String(update.id), update.scanner || null, update.ticker || null, update.deal_id || null, update]);
}

export async function upsertTradePostgres(t = {}) {
  if (!USE_PG) return;
  const pool = await getPool();
  await pool.query(`
    INSERT INTO trades(
      id,scanner,ticker,deal_id,direction,setup_type,status,entry,sl,tp1,tp2,close_price,pnl,
      risk_amount,quality_score,rsi,volume_ratio,htf_bias,spy_regime,vix_level,gap_pct,data,opened_at,closed_at,
      max_favorable,max_adverse,mae_r,mfe_r,intended_entry,initial_sl,fill_slippage_pct,excluded_from_expectancy,config_hash,
      spread_cost,commission,financing_accrued,pnl_gross,pnl_net,signal_to_order_ms,order_to_fill_ms,engine_branch
    )
    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,$35,$36,$37,$38,$39,$40,$41)
    ON CONFLICT(id) DO UPDATE SET
      scanner=$2,ticker=$3,deal_id=$4,direction=$5,setup_type=$6,status=$7,entry=$8,sl=$9,tp1=$10,tp2=$11,
      close_price=$12,pnl=$13,risk_amount=$14,quality_score=$15,rsi=$16,volume_ratio=$17,htf_bias=$18,
      spy_regime=$19,vix_level=$20,gap_pct=$21,data=$22,opened_at=$23,closed_at=$24,max_favorable=$25,
      max_adverse=$26,mae_r=$27,mfe_r=$28,intended_entry=$29,initial_sl=$30,fill_slippage_pct=$31,
      excluded_from_expectancy=$32,config_hash=$33,spread_cost=$34,commission=$35,financing_accrued=$36,pnl_gross=$37,pnl_net=$38,signal_to_order_ms=$39,order_to_fill_ms=$40,engine_branch=$41
  `, [
    String(t.id), t.scanner || null, t.ticker || null, t.deal_id || null, t.direction || null,
    t.setup_type || t.trade_type || null, t.status || null, t.entry ?? t.entry_price ?? null,
    t.sl ?? t.stop_loss ?? null, t.tp1 ?? t.take_profit_1 ?? null, t.tp2 ?? t.take_profit_2 ?? null,
    t.close_price ?? null, t.pnl ?? null, t.risk_amount ?? null, t.quality_score ?? t.signal_score ?? null,
    t.rsi ?? null, t.volume_ratio ?? null, t.htf_bias || null, t.spy_regime || null, t.vix_level ?? null,
    t.gap_pct ?? null, t, t.opened_at || t.ts || null, t.closed_at || null, t.max_favorable ?? null,
    t.max_adverse ?? null, t.mae_r ?? null, t.mfe_r ?? null, t.intended_entry ?? null, t.initial_sl ?? null,
    t.fill_slippage_pct ?? null, t.excluded_from_expectancy === true, t.config_hash || null,
    t.spread_cost ?? null,t.commission ?? 0,t.financing_accrued ?? 0,t.pnl_gross ?? null,t.pnl_net ?? null,
    t.signal_to_order_ms ?? null,t.order_to_fill_ms ?? null,t.engine_branch || null
  ]);
}

// Reservation writes on the /api/risk/check hot path must NOT go through
// saveToPostgres(): that syncs every collection (~12k serialized round-trips)
// inside one transaction and holds a DELETE lock on risk_ledger, which
// serializes concurrent risk checks. Measured 2026-08-08: 5 concurrent checks
// took 3.1/6.2/9.3/12.5/15.6s. These two helpers touch only the one row.
export async function insertReservationPostgres(r = {}) {
  if (!USE_PG) return;
  const pool = await getPool();
  await pool.query(`
    INSERT INTO risk_ledger(deal_id,scanner,ticker,risk_amount,opened_at,direction,asset_class,status,expires_at,reservation_id)
    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
    ON CONFLICT(deal_id) DO UPDATE SET scanner=$2,ticker=$3,risk_amount=$4,opened_at=$5,direction=$6,asset_class=$7,status=$8,expires_at=$9,reservation_id=$10
  `, [r.deal_id, r.scanner, r.ticker, r.risk_amount, r.opened_at || new Date(),
      r.direction || null, r.asset_class || null, r.status || 'PENDING',
      r.expires_at || null, r.reservation_id || null]);
}

// Converting a reservation into an open position is 1 DELETE + 1 UPSERT, not a
// full-database sync. Both in one small transaction so a crash cannot leave the
// reservation consumed without the position recorded.
export async function openPositionPostgres(row = {}, pendingDealId = null) {
  if (!USE_PG) return;
  const pool = await getPool();
  const c = await pool.connect();
  try {
    await c.query('BEGIN');
    if (pendingDealId && pendingDealId !== row.deal_id) {
      await c.query('DELETE FROM risk_ledger WHERE deal_id=$1', [pendingDealId]);
    }
    await c.query(`
      INSERT INTO risk_ledger(deal_id,scanner,ticker,risk_amount,opened_at,direction,asset_class,status,expires_at,reservation_id)
      VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
      ON CONFLICT(deal_id) DO UPDATE SET scanner=$2,ticker=$3,risk_amount=$4,opened_at=$5,direction=$6,asset_class=$7,status=$8,expires_at=$9,reservation_id=$10
    `, [row.deal_id, row.scanner, row.ticker, row.risk_amount, row.opened_at || new Date(),
        row.direction || null, row.asset_class || null, row.status || 'OPEN',
        row.expires_at || null, row.reservation_id || null]);
    await c.query('COMMIT');
  } catch (e) {
    await c.query('ROLLBACK').catch(() => {});
    throw e;
  } finally {
    c.release();
  }
}

// Targeted single-row delete for /api/risk/close. Returns true if a row was
// actually removed, so the caller can skip save() entirely on a no-op close
// (a nonexistent deal_id previously paid the full ~3.5s save() for nothing).
export async function closePositionPostgres(dealId) {
  if (!USE_PG) return false;
  const pool = await getPool();
  const { rowCount } = await pool.query('DELETE FROM risk_ledger WHERE deal_id=$1', [dealId]);
  return rowCount > 0;
}

// Returns the deal_ids actually removed so the caller can log real counts.
export async function sweepExpiredReservationsPostgres() {
  if (!USE_PG) return [];
  const pool = await getPool();
  const { rows } = await pool.query(
    `DELETE FROM risk_ledger WHERE status='PENDING' AND expires_at <= now() RETURNING deal_id`);
  return rows.map(r => r.deal_id);
}

export async function insertBrainPostgres(brain = {}) {
  if (!USE_PG) return;
  const pool = await getPool();
  await pool.query(`
    INSERT INTO trade_brain(id,scanner,setup_type,direction,ticker,deal_id,features,win,r_multiple,pnl,recorded_at)
    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
    ON CONFLICT(id) DO NOTHING
  `, [
    brain.id, brain.scanner || null, brain.setup_type || null, brain.direction || null, brain.ticker || null,
    brain.deal_id || null, brain.features || {}, brain.outcome?.win ?? brain.win ?? null,
    brain.outcome?.r_multiple ?? brain.r_multiple ?? null, brain.outcome?.pnl ?? brain.pnl ?? null,
    brain.recorded_at || new Date()
  ]);
}
