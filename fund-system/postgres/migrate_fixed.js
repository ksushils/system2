import pg from "pg";
import { readFileSync } from "fs";

const FUND_JSON = process.env.FUND_JSON || "/root/fund-system/data/fund.json";
const PG_URL = process.env.DATABASE_URL || "postgresql://funduser:CHANGEME@localhost:5432/funddb";

const pool = new pg.Pool({ connectionString: PG_URL });
const db = JSON.parse(readFileSync(FUND_JSON, "utf8"));

const q = (text, params) => pool.query(text, params);

async function run() {
  console.log("Migrating fund.json -> Postgres...\n");

  const validInvestors = new Set((db.investors || []).map(i => i.id));

  if (db.fund)           await q(`INSERT INTO fund_config(id,data) VALUES(1,\$1) ON CONFLICT(id) DO UPDATE SET data=\$1,updated_at=now()`, [db.fund]);
  if (db.scanner_config) await q(`INSERT INTO scanner_config(id,data) VALUES(1,\$1) ON CONFLICT(id) DO UPDATE SET data=\$1,updated_at=now()`, [db.scanner_config]);
  console.log("✓ config");

  for (const i of db.investors||[]) {
    await q(`INSERT INTO investors(id,name,email,pin,active,data) VALUES(\$1,\$2,\$3,\$4,\$5,\$6) ON CONFLICT(id) DO UPDATE SET name=\$2,email=\$3,pin=\$4,active=\$5,data=\$6`,
            [i.id, i.name, i.email||null, i.pin||null, i.active!==false, i]);
  }
  console.log(`✓ ${(db.investors||[]).length} investors`);

  const jsonbTable = async (rows, table, cols) => {
    for (const r of rows||[]) {
      const vals = cols.map(c => c==="data" ? r : (r[c] ?? null));
      const ph = cols.map((_,i)=>`\$${i+1}`).join(",");
      await q(`INSERT INTO ${table}(${cols.join(",")}) VALUES(${ph}) ON CONFLICT(${cols[0]}) DO NOTHING`, vals);
    }
    console.log(`✓ ${(rows||[]).length} ${table}`);
  };

  await jsonbTable(db.stakes,            "stakes",            ["id","investor_id","amount","type","data"]);
  await jsonbTable(db.allocations,       "allocations",       ["id","investor_id","data"]);
  await jsonbTable(db.withdrawals,       "withdrawals",       ["id","investor_id","amount","status","data"]);
  await jsonbTable(db.fees,              "fees",              ["id","data"]);
  await jsonbTable(db.monthly_snapshots, "monthly_snapshots", ["id","data"]);
  await jsonbTable(db.signals,           "signals",           ["id","scanner","ticker","data"]);
  await jsonbTable(db.rejections,        "rejections",        ["id","scanner","ticker","data"]);
  await jsonbTable(db.updates,           "updates",           ["id","scanner","ticker","deal_id","data"]);
  await jsonbTable(db.pings,             "pings",             ["id","scanner","data"]);

  // Risk settings — skip invalid investor_ids
  let rsCount = 0;
  for (const [iid, rs] of Object.entries(db.risk_settings||{})) {
    if (!validInvestors.has(iid)) {
      console.log(`  ⚠ skipping risk_settings for missing investor ${iid}`);
      continue;
    }
    await q(`INSERT INTO risk_settings(investor_id,data) VALUES(\$1,\$2) ON CONFLICT(investor_id) DO UPDATE SET data=\$2`, [iid, rs]);
    rsCount++;
  }
  if (Array.isArray(db.risk_settings)) {
    for (const rs of db.risk_settings) {
      if (!validInvestors.has(rs.investor_id)) {
        console.log(`  ⚠ skipping risk_settings for missing investor ${rs.investor_id}`);
        continue;
      }
      await q(`INSERT INTO risk_settings(investor_id,data) VALUES(\$1,\$2) ON CONFLICT(investor_id) DO UPDATE SET data=\$2`, [rs.investor_id, rs]);
      rsCount++;
    }
  }
  console.log(`✓ ${rsCount} risk_settings`);

  for (const t of db.trades||[]) {
    await q(`INSERT INTO trades(id,scanner,ticker,deal_id,direction,setup_type,status,entry,sl,tp1,tp2,close_price,pnl,risk_amount,quality_score,rsi,volume_ratio,htf_bias,spy_regime,vix_level,gap_pct,data,opened_at,closed_at)
             VALUES(\$1,\$2,\$3,\$4,\$5,\$6,\$7,\$8,\$9,\$10,\$11,\$12,\$13,\$14,\$15,\$16,\$17,\$18,\$19,\$20,\$21,\$22,\$23,\$24)
             ON CONFLICT(id) DO UPDATE SET status=\$7,close_price=\$12,pnl=\$13,closed_at=\$24,data=\$22`,
      [t.id, t.scanner, t.ticker, t.deal_id||null, t.direction||null, t.setup_type||t.trade_type||null,
       t.status||null, t.entry??t.entry_price??null, t.sl??t.stop_loss??null, t.tp1??t.take_profit_1??null,
       t.tp2??t.take_profit_2??null, t.close_price??null, t.pnl??null, t.risk_amount??null,
       t.quality_score??t.signal_score??null, t.rsi??null, t.volume_ratio??null, t.htf_bias||null,
       t.spy_regime||null, t.vix_level??null, t.gap_pct??null, t, t.opened_at||t.ts||null, t.closed_at||null]);
  }
  console.log(`✓ ${(db.trades||[]).length} trades`);

  for (const r of db.risk_ledger||[]) {
    await q(`INSERT INTO risk_ledger(deal_id,scanner,ticker,risk_amount,opened_at) VALUES(\$1,\$2,\$3,\$4,\$5) ON CONFLICT(deal_id) DO NOTHING`, [r.deal_id, r.scanner, r.ticker, r.risk_amount, r.opened_at||new Date()]);
  }
  console.log(`✓ ${(db.risk_ledger||[]).length} risk_ledger`);

  for (const [scanner, h] of Object.entries(db.heartbeats||{})) {
    await q(`INSERT INTO heartbeats(scanner,ts,status,msg) VALUES(\$1,\$2,\$3,\$4) ON CONFLICT(scanner) DO UPDATE SET ts=\$2,status=\$3,msg=\$4,updated_at=now()`,
            [scanner, h.ts, h.status, h.msg]);
  }
  console.log(`✓ heartbeats`);

  for (const b of db.trade_brain||[]) {
    await q(`INSERT INTO trade_brain(id,scanner,setup_type,direction,ticker,deal_id,features,win,r_multiple,pnl,recorded_at)
             VALUES(\$1,\$2,\$3,\$4,\$5,\$6,\$7,\$8,\$9,\$10,\$11) ON CONFLICT(id) DO NOTHING`,
      [b.id, b.scanner, b.setup_type, b.direction, b.ticker, b.deal_id||null,
       b.features, b.outcome?.win, b.outcome?.r_multiple, b.outcome?.pnl, b.recorded_at]);
  }
  console.log(`✓ ${(db.trade_brain||[]).length} trade_brain memories`);

  console.log("\n✅ Migration complete.");
  await pool.end();
}
run().catch(e => { console.error("Migration failed:", e.message); process.exit(1); });
