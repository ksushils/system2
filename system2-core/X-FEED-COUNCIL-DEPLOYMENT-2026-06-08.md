# X Feed and Council Deployment

Date: 2026-06-08
Mode: paper only

`SYSTEM2-ARCHITECTURE-v3.md` was not available in the workspace or Downloads.
The deployment used `SYSTEM2-ARCHITECTURE-v2.md` and the current live handoff.

## Backup

```text
/root/system2-core/backups/x-council-20260608T063412Z
```

## Tasks 1-2: X Feed and Viewer Pages

Deployed:

```text
/root/fund-system/public/x-feed-viewer.html
/root/fund-system/public/council-viewer.html
/root/fund-system/public/system2-terminal.html
/root/fund-system/server/scoring-endpoints.cjs
/root/system2-core/x_candidate_extractor.py
/root/system2-core/run_phase_b_core_baseline.py
```

New routes:

```text
GET  /api/x-feed
POST /api/x-feed/run
POST /api/idea/:id/council
GET  /x-feed
GET  /council
```

Static serving was already enabled in the Express server.

The live runner now calls `x_candidate_extractor.py` before Stage 1. Rich X
records include the requested reason, universe, sample-post, analyst, follower,
and raw-tweet fields. Only in-universe names merge into the candidate pool.

Manual X run:

```text
tweets fetched: 20
candidates found: 15
in universe: 9
pump filtered: 0
candidate pool: 800
```

The viewer and dashboard were visually verified. The Overview status shows:

```text
X_FEED_STATUS: ok
X_CANDIDATES_TONIGHT: 15
COUNCIL_STATUS: not wired
```

## Task 3: Council of AIs

The existing n8n workflow was updated in place:

```text
workflow id: a0vh8jfd4FtLZ0RJ
name: Council of AIs - System 2 Ride-Along
active: false
stored schedule: 30 3 * * 2-6
```

It now has the requested combined Chronos/Kronos prompt, three adversarial
personas, strict parsing fallbacks, verdict arithmetic, `models_available`,
and `council_gates_trades=false`.

n8n variables configured without printing values:

```text
ACCOUNT_SIZE
MAX_TRADES
FMP_API_KEY
TELEGRAM_CHAT_ID
TELEGRAM_BOT_TOKEN
```

Missing model variables:

```text
OPENAI_API_KEY
GEMINI_API_KEY
ANTHROPIC_API_KEY
```

Because all model keys are missing, the required real-model RPRX/SYY/FERG
test could not run. The fail-open test returned null raw responses, explicit
unavailable/parse fallbacks, `models_available=0`, TIER3, and
`remove_from_list=false` for all three. These are test fallbacks, not model
opinions.

The Council update endpoint was verified on the existing TEST paper record.
All requested fields persisted successfully.

The workflow was deliberately not activated and no council cron was added.

## Estimated Cost

Assuming roughly 2,500 input tokens and 150 output tokens per model per
finalist, 15 finalists would cost about:

```text
GPT-4o:             $0.12
Gemini 2.5 Flash:   $0.02
Claude Sonnet 4:    $0.15
Total:              about $0.28/night
```

At the configured 500-token output maximum for every response, the estimate
is below roughly $0.43/night. Actual cost depends on token usage and free-tier
eligibility.

## VPS Commands Used

Upload:

```powershell
scp -i C:\projects\system2-deploy\codex_system2_vps_key `
  C:\projects\system2-deploy\public\x-feed-viewer.html `
  root@72.62.134.167:/root/fund-system/public/x-feed-viewer.html

scp -i C:\projects\system2-deploy\codex_system2_vps_key `
  C:\projects\system2-deploy\public\council-viewer.html `
  root@72.62.134.167:/root/fund-system/public/council-viewer.html
```

Restart and tests:

```bash
pm2 restart fund-system --update-env
curl http://localhost:3210/api/x-feed
curl -I http://localhost:3210/x-feed
curl -I http://localhost:3210/council
curl -X POST http://localhost:3210/api/x-feed/run
```

Council verification:

```bash
docker exec n8n-n8n-1 n8n export:workflow \
  --id=a0vh8jfd4FtLZ0RJ --output=/tmp/final-council.json

cd /root/system2-core
python3 system2_council_smoke_test.py \
  --tickers RPRX,SYY,FERG --limit 3
```

No broker API, order execution, live trading, funnel gate, or live sizing
change was added.
