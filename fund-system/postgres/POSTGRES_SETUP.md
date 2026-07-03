# Postgres Migration — Setup Guide

Move the fund system from `fund.json` (lowdb) to Postgres on the SAME VPS.
No extra server. Postgres runs as a local service alongside your Node app.

## Why this helps
- **No more lost trades** from concurrent scanner writes (lowdb's last-write-wins)
- **Transactions** so investor money records can't half-write
- **Fast queries** as the brain grows to thousands of trades
- **pgvector-ready** if you later want semantic search on news/Gemini text

## ⚠️ Do this FIRST — protect today
Before migrating, set up the nightly backup so you always have a restore point:
```
cp backup-fund.sh /root/fund-system/backup-fund.sh
chmod +x /root/fund-system/backup-fund.sh
# add to crontab (runs 2am daily):
( crontab -l 2>/dev/null; echo "0 2 * * * /root/fund-system/backup-fund.sh" ) | crontab -
# run once now to confirm:
/root/fund-system/backup-fund.sh
```

## Step 1 — Install Postgres on the VPS
```
apt-get update
apt-get install -y postgresql postgresql-contrib
systemctl enable --now postgresql
```

## Step 2 — Create database + user
```
sudo -u postgres psql <<'SQL'
CREATE DATABASE funddb;
CREATE USER funduser WITH PASSWORD 'pick_a_strong_password';
GRANT ALL PRIVILEGES ON DATABASE funddb TO funduser;
\c funddb
GRANT ALL ON SCHEMA public TO funduser;
SQL
```

## Step 3 — Create tables
```
psql "postgresql://funduser:pick_a_strong_password@localhost:5432/funddb" -f schema.sql
```

## Step 4 — Migrate existing data
```
cd /root/fund-system
npm install pg          # add the Postgres driver
DATABASE_URL="postgresql://funduser:pick_a_strong_password@localhost:5432/funddb" \
FUND_JSON="/root/fund-system/data/fund.json" \
node migrate.js
```
Expected output: a ✓ line per table, ending "Migration complete."
Safe to re-run — uses upserts, won't duplicate.

## Step 5 — Point the server at Postgres
The new server build uses a storage adapter. Set the env var in your pm2 config
or ecosystem file:
```
DATABASE_URL=postgresql://funduser:pick_a_strong_password@localhost:5432/funddb
USE_POSTGRES=true
```
Then `pm2 restart fund-system`.

## Step 6 — Safety net (run BOTH for one week)
Keep `fund.json` writes ON alongside Postgres for ~1 week (the adapter does this
when `DUAL_WRITE=true`). Compare a few records, confirm everything matches, then
set `DUAL_WRITE=false` to go Postgres-only.

## Rollback
If anything goes wrong: set `USE_POSTGRES=false`, `pm2 restart fund-system`.
You're instantly back on fund.json with zero data loss (it was still being written).

## Verify after cutover
```
psql "$DATABASE_URL" -c "SELECT count(*) FROM trades;"
psql "$DATABASE_URL" -c "SELECT count(*) FROM trade_brain;"
psql "$DATABASE_URL" -c "SELECT count(*) FROM investors;"
```
Numbers should match what you see in the dashboard.

## ✅ Server is now wired for Postgres
The new `server-index.js` already imports the storage adapter. Behaviour is
controlled entirely by environment variables — NO code changes needed to switch:

| USE_POSTGRES | DUAL_WRITE | Behaviour |
|---|---|---|
| (unset) | (unset) | Runs on fund.json exactly as before. pg package not even required. |
| true | true | Loads from Postgres, writes BOTH Postgres + fund.json (safety week) |
| true | false | Postgres only (final state after you've verified) |

### Deploy steps
1. Upload BOTH files to the VPS:
   - `server-index.js`     → `/root/fund-system/server/index.js`
   - `storage-adapter.js`  → `/root/fund-system/server/storage-adapter.js`  ← MUST sit next to index.js
2. While still on JSON (USE_POSTGRES unset), `pm2 restart fund-system` — confirm everything works unchanged.
3. Install Postgres + run schema.sql + migrate.js (steps 1–4 above).
4. `npm install pg` in /root/fund-system
5. Set env vars in your pm2 ecosystem file:
   ```
   USE_POSTGRES=true
   DUAL_WRITE=true
   DATABASE_URL=postgresql://funduser:yourpass@localhost:5432/funddb
   ```
6. `pm2 restart fund-system`. Boot log should say "✓ Loaded data from Postgres".
7. Run for ~1 week with DUAL_WRITE=true. Compare row counts (verify queries above).
8. Set DUAL_WRITE=false → Postgres-only. Keep nightly fund.json backup running regardless.

### If anything breaks
Set USE_POSTGRES=false (or remove it), `pm2 restart`. Instantly back on fund.json.
The adapter is a clean no-op when off — it doesn't even load the pg package.
