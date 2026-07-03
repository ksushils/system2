-- ════════════════════════════════════════════════════════════
-- Alpha Bot Fund — Postgres schema
-- Matches the existing fund.json data model exactly.
-- Run once: psql -U funduser -d funddb -f schema.sql
-- ════════════════════════════════════════════════════════════

-- Singleton config tables (one row each) stored as JSONB for flexibility
CREATE TABLE IF NOT EXISTS fund_config (
  id            INT PRIMARY KEY DEFAULT 1,
  data          JSONB NOT NULL,
  updated_at    TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT singleton_fund CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS scanner_config (
  id            INT PRIMARY KEY DEFAULT 1,
  data          JSONB NOT NULL,
  updated_at    TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT singleton_scanner CHECK (id = 1)
);

-- Investors
CREATE TABLE IF NOT EXISTS investors (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  email         TEXT,
  pin           TEXT,
  active        BOOLEAN DEFAULT true,
  data          JSONB,                    -- any extra fields
  created_at    TIMESTAMPTZ DEFAULT now()
);

-- Capital events (stakes)
CREATE TABLE IF NOT EXISTS stakes (
  id            TEXT PRIMARY KEY,
  investor_id   TEXT REFERENCES investors(id) ON DELETE CASCADE,
  amount        NUMERIC,
  type          TEXT,
  data          JSONB,
  created_at    TIMESTAMPTZ DEFAULT now()
);

-- Allocations per investor per scanner
CREATE TABLE IF NOT EXISTS allocations (
  id            TEXT PRIMARY KEY,
  investor_id   TEXT REFERENCES investors(id) ON DELETE CASCADE,
  data          JSONB,
  created_at    TIMESTAMPTZ DEFAULT now()
);

-- Risk settings per investor
CREATE TABLE IF NOT EXISTS risk_settings (
  investor_id   TEXT PRIMARY KEY REFERENCES investors(id) ON DELETE CASCADE,
  data          JSONB
);

-- Trades (the hot path — needs real columns for querying)
CREATE TABLE IF NOT EXISTS trades (
  id            TEXT PRIMARY KEY,
  scanner       TEXT,
  ticker        TEXT,
  deal_id       TEXT,
  direction     TEXT,
  setup_type    TEXT,
  status        TEXT,
  entry         NUMERIC,
  sl            NUMERIC,
  tp1           NUMERIC,
  tp2           NUMERIC,
  close_price   NUMERIC,
  pnl           NUMERIC,
  risk_amount   NUMERIC,
  quality_score NUMERIC,
  rsi           NUMERIC,
  volume_ratio  NUMERIC,
  htf_bias      TEXT,
  spy_regime    TEXT,
  vix_level     NUMERIC,
  gap_pct       NUMERIC,
  data          JSONB,                    -- full original payload
  opened_at     TIMESTAMPTZ,
  closed_at     TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trades_scanner ON trades(scanner);
CREATE INDEX IF NOT EXISTS idx_trades_status  ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_deal    ON trades(deal_id);

-- Signals, rejections, updates, pings — append-only logs
CREATE TABLE IF NOT EXISTS signals    (id TEXT PRIMARY KEY, scanner TEXT, ticker TEXT, data JSONB, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS rejections (id TEXT PRIMARY KEY, scanner TEXT, ticker TEXT, data JSONB, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS updates    (id TEXT PRIMARY KEY, scanner TEXT, ticker TEXT, deal_id TEXT, data JSONB, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS pings      (id TEXT PRIMARY KEY, scanner TEXT, data JSONB, created_at TIMESTAMPTZ DEFAULT now());

-- Withdrawals & fees
CREATE TABLE IF NOT EXISTS withdrawals (id TEXT PRIMARY KEY, investor_id TEXT, amount NUMERIC, status TEXT, data JSONB, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS fees        (id TEXT PRIMARY KEY, data JSONB, created_at TIMESTAMPTZ DEFAULT now());

-- Monthly snapshots & sessions
CREATE TABLE IF NOT EXISTS monthly_snapshots (id TEXT PRIMARY KEY, data JSONB, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS sessions          (token TEXT PRIMARY KEY, role TEXT, investor_id TEXT, data JSONB, created_at TIMESTAMPTZ DEFAULT now());

-- Risk ledger (live open risk across scanners)
CREATE TABLE IF NOT EXISTS risk_ledger (
  deal_id       TEXT PRIMARY KEY,
  scanner       TEXT,
  ticker        TEXT,
  risk_amount   NUMERIC,
  opened_at     TIMESTAMPTZ DEFAULT now()
);

-- Heartbeats (one row per scanner)
CREATE TABLE IF NOT EXISTS heartbeats (
  scanner       TEXT PRIMARY KEY,
  ts            BIGINT,
  status        TEXT,
  msg           TEXT,
  updated_at    TIMESTAMPTZ DEFAULT now()
);

-- ── TRADE BRAIN — with optional pgvector for future semantic search ──
CREATE TABLE IF NOT EXISTS trade_brain (
  id            TEXT PRIMARY KEY,
  scanner       TEXT,
  setup_type    TEXT,
  direction     TEXT,
  ticker        TEXT,
  deal_id       TEXT,
  features      JSONB,                    -- the bucketed feature set
  win           BOOLEAN,
  r_multiple    NUMERIC,
  pnl           NUMERIC,
  recorded_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_brain_setup   ON trade_brain(setup_type);
CREATE INDEX IF NOT EXISTS idx_brain_scanner ON trade_brain(scanner);
-- GIN index on features lets us query by bucket fast:
CREATE INDEX IF NOT EXISTS idx_brain_features ON trade_brain USING GIN (features);

-- pgvector (optional, future): only if you later embed news/Gemini text
-- CREATE EXTENSION IF NOT EXISTS vector;
-- ALTER TABLE trade_brain ADD COLUMN embedding vector(384);
-- CREATE INDEX ON trade_brain USING ivfflat (embedding vector_cosine_ops);
