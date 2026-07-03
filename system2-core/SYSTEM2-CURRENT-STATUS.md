# System 2 Current Status / Codex Handoff

Last updated: 2026-06-08

Use this file to continue System 2 work from a new Codex/OpenAI account.

## How To Resume

Open this folder in Codex:

```text
C:\projects\system2-deploy
```

Tell Codex:

```text
Read C:\projects\system2-deploy\SYSTEM2-CURRENT-STATUS.md first, then continue from the pending task. Paper mode only. Do not deploy or activate anything unless I say.
```

Also read the architecture doc before changing System 2:

```text
C:\Users\ksush\Downloads\SYSTEM2-ARCHITECTURE-v2.md
```

## Prime Rules

- Paper mode only. No live trading.
- Do not call broker APIs.
- Do not wire live execution.
- Core scanner/risk logic is the foundation.
- Edge layers are ride-along and logged until the scoring loop proves value.
- Do not let unproven layers gate, reject, or rerank trades unless explicitly approved.
- Do not deploy/activate a preview change until the user reviews the output.

## VPS / Service Map

VPS:

```text
root@72.62.134.167
SSH key: C:\projects\system2-deploy\codex_system2_vps_key
```

Important VPS paths:

```text
/root/system2-core
/root/system2-core-preview-1500
/root/fund-system
/root/fund-system/server
/root/fund-system/public
```

Live dashboard:

```text
http://72.62.134.167:3210
```

Chronos service:

```text
http://72.62.134.167:8000/healthz
http://72.62.134.167:8000/forecast
```

Kronos service:

```text
http://72.62.134.167:8001/healthz
http://72.62.134.167:8001/forecast
```

Options flow service:

```text
http://72.62.134.167:8002/healthz
http://72.62.134.167:8002/options-flow
```

n8n:

```text
n8n container on VPS: n8n-n8n-1
n8n URL: https://n8n.srv1282556.hstgr.cloud
```

Important: inside n8n, do not use localhost for VPS services. Use `http://72.62.134.167:<port>`.

## Current Cron State

Current VPS crontab includes:

```text
CRON_TZ=UTC
15 2 * * 2-6 /root/system2-core/run_phase_b_core_baseline.sh

0 14-21 * * 1-5 /root/system2-core/run_system2_paper_monitor.sh

CRON_TZ=UTC
0 14 * * 1-5 /root/system2-core/premarket_gap_check.py --apply --send-alerts >> /root/system2-core/logs/premarket.log 2>&1

CRON_TZ=UTC
0 15 * * 1-5 /root/system2-core/run_open_confirmation.sh
```

The nightly runner is live on `/root/system2-core`.

## Environment / Secrets Status

Do not print secret values.

Known env file:

```text
/root/system2-core/.env
```

Known key presence as of last check:

```text
FMP_API_KEY present
TELEGRAM_BOT_TOKEN present
TELEGRAM_CHAT_ID present
ANTHROPIC_API_KEY missing
OPENAI_API_KEY missing
GEMINI_API_KEY missing
GETXAPI_KEY present
```

Telegram was configured and API-tested on 2026-06-07. A paper-mode test
message was accepted successfully by Telegram.

Council real-model testing is blocked until OpenAI and Gemini keys are added.
Anthropic remains optional.

X/FinTwit discovery is active in the paper runner and writes validated names
to the candidate pool.

## X Feed and Council Viewer Update

Deployed on 2026-06-08:

```text
GET  /api/x-feed
POST /api/x-feed/run
POST /api/idea/:id/council
GET  /x-feed
GET  /council
```

The dashboard has external X Feed and Council tabs plus Overview health rows.
The X extractor now writes rich viewer fields and runs before Stage 1. Its
manual acceptance run found 15 names, 9 in universe, while preserving the
800-name candidate-pool count.

Council n8n workflow `a0vh8jfd4FtLZ0RJ` is configured but inactive. Its stored
schedule is `30 3 * * 2-6`, its gates remain false, and no council cron was
activated. FMP/Telegram/account variables are configured in n8n. OpenAI,
Gemini, and Anthropic keys are all still missing, so the three-model test and
nightly activation remain blocked.

Backup:

```text
/root/system2-core/backups/x-council-20260608T063412Z
```

## What Is Live

### Confluence ranking

Confluence ranking is deployed in the live paper runner:

```text
/root/system2-core/confluence_scoring.py
```

It runs after Chronos and options enrichment and before the Stage 7 cluster
guard. It re-ranks only the immutable Stage 2 top 40. Position 41+ cannot
enter through confluence.

Live fields:

```text
setup_score
confluence_score
confluence_bonuses
confluence_rank
```

The dashboard displays setup and confluence scores, defaults finalist sorting
to confluence, supports a setup-score toggle, and reports high/medium/low
confluence outcome cohorts.

Manual verification on the last enriched top 40 passed:

```text
input: 40
output: 40
eligible: 40
outside names considered: 0
membership unchanged: true
```

The full manual runner attempt on 2026-06-07 was correctly aborted by the
existing `RISK_OFF` regime kill switch. The confluence stage was then tested
directly against the last completed enriched top 40. No safety rule was
bypassed.

### Fund dashboard/scoring loop

`fund-system` is running under PM2.

Scoring endpoints are installed in:

```text
/root/fund-system/server/scoring-endpoints.cjs
```

Known endpoints:

```text
POST /api/idea
POST /api/score/run
GET  /api/score/stats
GET  /api/ideas
GET  /api/stage-detail
POST /api/system2/pre-market-gap/run-local
POST /api/system2/pre-market-gap/run
```

Admin/token protection exists on some dashboard endpoints. Local-only routes work from the VPS.

### Phase B bare core

The bare-core pipeline is on the VPS:

```text
/root/system2-core/run_phase_b_core_baseline.py
```

It currently runs:

1. Regime check
2. Universe builder
3. Stage 1 cheap filter
4. Stage 2 Surgical Strike technical score
5. Chronos ride-along
6. Options ride-along
7. Stage 7 correlation clustering
8. Stage 5 news safety
9. Log paper ideas to `/api/idea`
10. Record run/stage metadata

Paper mode only.

### Corrected ATR stops

Stage 2 live file:

```text
/root/system2-core/b3_surgical_strike_stage2.py
```

Important fix already activated:

- 5-minute candles still drive VWAP/RVOL/momentum scoring.
- Daily OHLCV drives ATR14 stop/target/position sizing.
- Daily ATR is fetched only for the top 40.
- Stop sizing uses daily ATR, not 5-minute ATR.
- Sanity checks: wide stop warning, position_too_small reject, 1000-share cap.

User confirmed this fix.

### Regime kill switch

Regime check is wired first in the runner:

```text
/root/system2-core/regime_check.py
```

Rules:

- On `RISK_OFF`, abort scan, log metadata, send Telegram if configured, and do not post ideas.
- On `CAUTION`, continue, tag ideas with caution regime info.
- On `TRENDING` or `NEUTRAL`, continue normally.

June 5 risk-off check was confirmed correct by user.

### Pre-market gap check / Signal 1

Live script:

```text
/root/system2-core/premarket_gap_check.py
```

Status:

- Built and deployed.
- Cron active at 14:00 UTC Mon-Fri.
- Uses FMP `/stable/quote?symbol=TICKER`.
- Reads `preMarketPrice` first when available.
- Flags:
  - `pre_market_gap_adverse=true` if move is more than `1.5x ATR` against setup.
  - `pre_market_gap_favourable=true` if move is more than `1.5x ATR` with setup.
- Logs:
  - `pre_market_checked_at`
  - `pre_market_price`
  - `pre_market_price_source`
  - `pre_market_gap_pct`
  - `pre_market_gap_atr_multiple`
  - `pre_market_gap_threshold_atr`
  - `pre_market_gap_adverse`
  - `pre_market_gap_favourable`
  - `pre_market_gap_error`

Dry-run result on 2026-06-06:

```text
open ideas checked: 30
FMP calls: 30
classification_counts: NORMAL 30
alerts_that_would_fire: 0
```

Synthetic test passed:

- adverse 1.75x ATR generated the exact pre-market alert.
- favourable 1.75x ATR set `pre_market_gap_favourable=true`.

Blocked until true live alerting:

```text
TELEGRAM_CHAT_ID missing
```

Do not start Signal 2 seasonality until Signal 1 is confirmed on a real Monday pre-market run unless user explicitly says to proceed.

## What Is Built But Not Activated

### Council of AIs v5.1

Existing n8n workflow confirmed:

```text
eEU1gnO9tkcBFwHR | Council of AIs — Surgical Strike v5.0
```

Imported inactive n8n workflow:

```text
a0vh8jfd4FtLZ0RJ | Council of AIs - Surgical Strike v5.1 - Ride-Along Council Logging
active=false
```

Generated workflow artifacts:

```text
/root/system2-core/council-staging/council-of-ais-v5.1-ridealong-council-logging.json
C:\projects\system2-deploy\council-of-ais-v5.1-ridealong-council-logging.json
```

It includes:

- strict senior trade reviewer system prompt
- per-stock user prompt
- Claude/GPT/Gemini calls in parallel-style branches
- JSON parse fallback
- verdict engine
- `council_gates_trades = false`
- only `FORCE_SKIP` hard-removes
- `/api/idea` council field logging
- `setup_score` and `confluence_score` preserved in `/api/idea`
- Telegram council text

Smoke test script:

```text
/root/system2-core/system2_council_smoke_test.py
C:\projects\system2-deploy\system2_council_smoke_test.py
```

Blocked:

```text
ANTHROPIC_API_KEY missing
OPENAI_API_KEY present
GEMINI_API_KEY present
TELEGRAM_BOT_TOKEN present
TELEGRAM_CHAT_ID present
```

Do not activate Council until a real 3-ticker model smoke test returns actual JSON from all three models.

### X/FinTwit existing workflow audit

Existing n8n workflow found:

```text
4hLGZX6FojpVDXOm | X Social Momentum Scanner v7.1 - FIXED Google Sheets
active: false
nodes: 29
```

Exported:

```text
/root/system2-core/x-social-momentum-v7.1.json
C:\projects\system2-deploy\x-social-momentum-v7.1.json
```

Findings:

- Uses GetXAPI, not official X API.
- Endpoint:

```text
https://api.getxapi.com/twitter/tweet/advanced_search
Authorization: Bearer {{$env.GETXAPI_KEY}}
```

- Scrapes trusted financial accounts, rotating cashtag batches, and catalyst keyword searches.
- Emits ranked social candidates, sample raw posts, trusted authors, social score, Gemini sentiment, and Google Sheets rows.
- It also has paper-trading logic; do not reuse that part for System 2.
- Workflow currently has a market-hours gate 8am-5pm ET and would likely skip at 22:00 ET.
- Query volume is too high for System 2 nightly use; should be made lean if approved.

Blocked:

```text
GETXAPI_KEY not found in container env or VPS files
```

User has not approved wiring X yet. Do not build past the audit until user confirms.

## Current Pending Task: Universe Expansion 800 -> 1,500

User requested:

- Change `universe_builder.py` only.
- Update n8n universe-builder node too, if applicable.
- Target size 1,500.
- Do not change Stage 2/3/4/5/6/7/scoring logic.
- Run Stage 1 impact check.
- Show universe and Stage 1 results before activating the new universe in nightly cron.

Important: this was started in preview only. It is not activated in live cron.

### Local files updated

Updated locally:

```text
C:\projects\system2-deploy\universe_builder.py
C:\projects\system2-deploy\system2-config.json
```

`system2-config.json` universe section now:

```json
"universe": {
  "target_size": 1500,
  "min_market_cap": 1000000000,
  "min_avg_volume": 500000,
  "exclude_price_below": 5
}
```

### Preview folder on VPS

Preview path:

```text
/root/system2-core-preview-1500
```

This folder contains:

```text
universe_builder.py
system2-config.json
b2_stage1_cheap_filter.py
universe.json
universe.metadata.json
stage1_survivors.json
stage1_details.json
stage1_metadata.json
universe_builder_1500_run.log
stage1_1500_run.log
```

The live `/root/system2-core` was not changed for this task.

### Universe 1,500 preview results

Build completed successfully.

FMP endpoint behavior:

- `/stable/sp500_constituent` returned 404.
- `/stable/sp500-constituent` worked.
- `/stable/nasdaq_constituent` returned 404.
- `/stable/nasdaq-constituent` worked.
- `/stable/dowjones_constituent` returned 404.
- `/stable/dowjones-constituent` worked.
- `/stable/russell1000_constituent` and `/stable/russell1000-constituent` returned 404.
- Russell fallback used `/stable/company-screener`.
- `/stable/stock-screener` returned 404 on this FMP account.
- `/stable/company-screener` worked.

Universe summary:

```text
targetSize: 1500
finalCount: 1500
```

Source fetch counts before dedupe:

```json
{
  "sp500": 503,
  "nasdaq100": 101,
  "dow30": 30,
  "russell1000_screener_fallback": 1617,
  "liquid_midcap_pad": 1880
}
```

Source added counts after dedupe:

```json
{
  "sp500": 489,
  "nasdaq100": 14,
  "dow30": 0,
  "russell1000_screener_fallback": 997,
  "liquid_midcap_pad": 0
}
```

Counts:

```text
index/fallback added count: 1500
screener pad added count: 0
```

Note: because Russell 1000 endpoint is unavailable, the fallback screener filled the remaining universe before the mid-cap pad layer needed to add names.

Odd ticker check:

```json
{
  "AXIA": "AXIA Energia S.A.",
  "AMRZ": "Amrize Ltd",
  "SOLS": "Solstice Advanced Materials Inc.",
  "STRC": "NOT_IN_UNIVERSE",
  "SUNB": "Sunbelt Rentals Holdings Inc",
  "WSE": "Wise Group plc Class A Ordinary Shares",
  "MAIR": "Madison Air Solutions Corporation",
  "FPS": "Forgent Power Solutions, Inc.",
  "FDXF": "FedEx Freight Holding Company, Inc.",
  "MICC": "The Magnum Ice Cream Company N.V.",
  "PSKY": "Paramount Skydance Corp",
  "SGI": "Somnigroup International Inc"
}
```

First 30 sorted universe:

```text
A, AA, AADX, AAL, AAOI, AAON, AAP, AAPL, ABBV, ABM, ABNB, ABT, ABVX, ACAD, ACGL, ACHC, ACHR, ACI, ACIW, ACM, ACMR, ACN, ADBE, ADC, ADEA, ADI, ADM, ADP, ADPT, ADSK
```

Last 30 sorted universe:

```text
WYNN, XEL, XENE, XMTR, XOM, XP, XPEV, XPO, XYL, XYZ, YETI, YMM, YOU, YPF, YSS, YUM, YUMC, Z, ZBH, ZBRA, ZETA, ZG, ZGN, ZIM, ZION, ZM, ZS, ZTO, ZTS, ZWS
```

### Stage 1 preview results on 1,500 universe

The interrupted command completed on the VPS.

Stage 1 preview metadata:

```text
inputUniverseCount: 1500
survivorCount: 1226
rejectedCount: 274
totalFmpCalls: 1501
runtimeSeconds: 586.49
fmpErrorCount: 1
```

Rejection breakdown:

```json
{
  "removed_volume": 238,
  "removed_price": 0,
  "removed_dollar_vol": 23,
  "removed_earnings": 13,
  "removed_other": 0
}
```

This is higher than the user's expected ~900-1,000 survivors. It means the 1,500 universe is still mostly liquid under current Stage 1 rules.

Runtime was about 9.8 minutes for Stage 1 alone, not the expected +2-3 minutes. Reason: current Stage 1 does one profile call per ticker and sleeps 1 second every 75 names.

Do not activate until user reviews this.

### What To Do Next For Universe Task

1. Report the 1,500 universe preview and Stage 1 numbers to the user.
2. Ask whether to approve activation despite:
   - Stage 1 survivors = 1,226, higher than expected.
   - Stage 1 runtime = 586.49s, higher than expected.
3. If user approves, deploy these files from local or preview into live:

```text
/root/system2-core/universe_builder.py
/root/system2-core/system2-config.json
```

4. Do not change the cron schedule unless user asks.
5. Do not change Stage 1/2/3/4/5/6/7 scoring logic unless user explicitly requests optimization.

n8n audit result:

- No System 2 universe-builder node exists in n8n.
- The only System 2 workflow found was `System 2 — Daily Scorer`, with no
  universe node.
- Other n8n universe-builder nodes belong to legacy/System 1 FMP scanners and
  must not be changed for this task.

Possible optimization, not yet approved:

- Stage 1 could use FMP batch quote/profile/screener data to reduce calls and runtime, but that would be a Stage 1 implementation change, not a pure universe-builder change. Do not do it unless approved.

## Local Files Worth Knowing

Core local workspace:

```text
C:\projects\system2-deploy
```

Important files:

```text
universe_builder.py
system2-config.json
b2_stage1_cheap_filter.py
b3_surgical_strike_stage2.py
b4_correlation_cluster_engine.py
c1_options_flow_stage3_ridealong.py
c2_chronos_stage4_ridealong.py
c3_stage5_news_safety_filter.py
log_phase_b_baseline_ideas.py
run_phase_b_core_baseline.py
premarket_gap_check.py
regime_check.py
record_system2_run_metadata.py
record_system2_stage_details.py
scoring-endpoints.cjs
public/system2-terminal.html
phase_d_analyzer.py
test_system2.py
```

Temporary/preview metadata copied locally:

```text
universe_1500_preview.metadata.json
stage1_1500_preview.metadata.json
```

## Things Not To Forget

- Telegram bot token/chat ID are configured in `/root/system2-core/.env`.
- Council v5.1 is imported inactive; Anthropic API key is still missing.
- Kronos CPU feasibility benchmark completed on 2026-06-07. With 40 tickers,
  five forecast days, and `n_samples=10`, small took 368.303 seconds and base
  took 909.339 seconds. Peak process RAM was 0.876 GiB and 1.305 GiB.
- A persistent 2 GiB `/swapfile` is active on the VPS.
- Kronos-base FastAPI service is deployed under `/root/kronos-service`, managed
  by PM2 as `kronos-service`, and healthy on port 8001.
- Kronos defaults to `n_samples=10`, loads once at startup, and caches weights
  under `/root/kronos-service/weights`.
- The real NVDA/AAPL/FERG test passed. Two warm calls took 14.784 and 14.864
  seconds and retained the same model instance.
- Kronos Step 3 pipeline integration was activated in `/root/system2-core` on
  2026-06-07 after user approval. It remains paper mode only.
- Kronos Step 3 preview completed under `/root/system2-step3-preview`.
- Full manual paper run completed in 875.67 seconds with 40 forecast inputs,
  16 clustered finalists, and 16 successful `/api/idea` posts.
- Forecast timing was Chronos 13.552 seconds, Kronos 210.861 seconds, total
  224.413 seconds.
- All 33 combined bands above 5% received zero forecast confluence bonus.
- `/api/idea` and run-metadata persistence schemas now retain Kronos,
  combined-forecast, forecast-contribution, and timing fields.
- Fail-open tests passed for one-model and both-model outage cases.
- The live runner logs `chronos_inference_seconds`, `kronos_inference_seconds`,
  `total_forecast_seconds`, and `combined_forecast_fields_populated`.
- Chronos and Kronos health checks passed immediately after activation.
- The dashboard now shows Stage 5 timings, both forecasts on trade cards,
  color-coded cone widths, and a Stage 5 detail table sorted by combined band.
- Pre-activation core backups are in
  `/root/system2-core/backups/step3-20260607T182828Z`.
- Dashboard/server backups are
  `/root/fund-system/public/system2-terminal.html.bak-step3-20260607T182828Z`
  and `/root/fund-system/server/scoring-endpoints.cjs.bak-step3-20260607T182828Z`.
- Cron remains `15 2 * * 2-6`; the next scheduled run after activation is
  Tuesday, 2026-06-09 at 02:15 UTC.
- X discovery is active in the paper runner. The 2026-06-07 live test fetched
  20 posts and accepted AAPL, RBRK, MAR, and LLY into the candidate pool.
- Signal 2 seasonality and Signal 3 dark pool proxy are not started.
- The 1,500 universe preview is built and Stage 1 impact is complete but not activated.
- The live nightly baseline still uses `/root/system2-core`, not `/root/system2-core-preview-1500`.
- Targeted improvements Task 1, Fix 1 was activated in the live runner on
  2026-06-07 after preview confirmation.
- Fix 1 uses the normalized order and artifacts:
  Stage 2 technical -> `stage3_news_safe_top40.json` ->
  `stage4_options_enriched_top40.json` -> Stage 5 combined forecast ->
  Stage 6 council -> Stage 7 correlation.
- The 2026-06-07 Fix 1 preview passed 39 Stage 3 survivors into Stage 4 in
  identical order. One ticker, LMT, was removed by the unchanged news logic
  because a law-firm investigation headline matched `fraud_investigation`.
- Fix 1 backups are in
  `/root/system2-core/backups/fix1-20260607T190229Z`, plus:
  `/root/fund-system/public/system2-terminal.html.bak-fix1-20260607T190229Z`
  and `/root/fund-system/server/scoring-endpoints.cjs.bak-fix1-20260607T190229Z`.
- Live metadata order is now Universe, Stage 1, Stage 2, Stage 3 News,
  Stage 4 Options, Stage 5 Forecasts, Stage 6 Council, Stage 7 Correlation,
  Finalists. Cron remains unchanged.
- Task 1 Fix 2 was activated on 2026-06-07. Alpaca credentials are configured
  in `/root/system2-core/.env` with mode `0600`; do not print their values.
- The live scorer now uses Alpaca daily bars for immutable actual entry and
  Alpaca 1-minute bars for MFE/MAE and stop/target ordering, with FMP daily bars
  as fail-open fallback. Phase D uses actual R first and planned R for legacy
  records only.
- The Alpaca-vs-FMP copied-store comparison covered 19 scored ideas. Four
  (21.1%) differed by at least 0.5%; maximum absolute differences were 1.554%
  MFE and 1.432% MAE, with one hit-status change. Because this was material,
  the live activation retains the Alpaca minute-bar path.
- Fix 2 backups are in
  `/root/system2-core/backups/fix2-20260607T212846Z`.
- The dashboard scoreboard now shows Planned entry, Actual entry, Gap %,
  Planned R, Actual R, MFE%, MAE%, and SPY return. Statistics includes average
  entry gap and clearly identifies pending and legacy records.
- n8n workflow `System 2 — Daily Scorer` (`JbUOYzfnzCFuSRZP`) is active with
  its unchanged schedule `0 23 * * 1-5`. The next run is Monday,
  2026-06-08 at 23:00 UTC; it will populate weekend ideas from the Monday open.
