# Intelligence Capture System — Deployment Report
**Date:** 2026-06-10  
**Status:** DEPLOYED

---

## Files Created / Modified

| File | Action |
|------|--------|
| `/root/system2-core/intelligence_engine.py` | **Created** — Core engine with 4 modules |
| `/root/system2-core/nightly_learning.py` | **Updated** — Integrated intelligence into nightly flow |
| `/root/fund-system/server/scoring-endpoints.cjs` | **Updated** — Added `/api/system2/intelligence` endpoint |
| `/root/fund-system/public/system2-terminal.html` | **Updated** — Added Intelligence tab with 4 panels |
| `/root/fund-system/public/system2-terminal.html.bak.20260610_161549` | **Backup** — Dashboard backup before changes |

---

## Validation Results (All 7 Items)

### 1. ✅ Run intelligence_engine.py standalone
```bash
cd /root/system2-core && python3 intelligence_engine.py
```
**Results:**
- Shadow portfolio: 3,056 rejected ideas tracked, 16 gates with data
- Stage funnel: 5 pipeline runs analyzed, 100% success rate
- Source attribution: 20 resolved ideas, best source = `pre_market_gap_checked` (+3.93R lift)
- What-if analysis: 59 ideas, best strategy = `pre_market_favourable` (6.49R avg)

### 2. ✅ Intelligence tab renders in dashboard
- PERFORMANCE → Intelligence sub-tab added
- 4 panels render: Pipeline Health, Shadow Portfolio, Source Value, What-If
- "Insufficient data" messages shown gracefully where needed
- JS syntax validator passes: `node -e "new Function(script)"` → OK

### 3. ✅ API endpoint works
```bash
curl 'http://127.0.0.1:3210/api/system2/intelligence?token=...'
```
**Response:** `{"ok": true, "shadow_portfolio": {...}, "stage_funnel": {...}, ...}`

### 4. ✅ Nightly integration
```bash
cd /root/system2-core && python3 nightly_learning.py --intelligence-only
```
- Imports all 4 intelligence functions
- Saves `data/intelligence_report.json`
- Monthly report auto-triggers on 1st of month via `datetime.now(timezone.utc).day == 1`
- Post-mortem output includes intelligence summary keys

### 5. ✅ Shadow portfolio fetches real returns
- Cache file: `/root/system2-core/data/shadow_return_cache.json` (3,842 entries)
- Sample verified returns:
  - `AIZ_2026-06-05`: -2.47%
  - `BAP_2026-06-05`: +8.41%
  - `BLK_2026-06-05`: +1.64%
- Mixed cache format supported (legacy floats + new dicts with 5d/10d)

### 6. ✅ Monthly report generation
```bash
cd /root/system2-core && python3 intelligence_engine.py --monthly 2026-06
```
**Output:** `/root/system2-core/reports/monthly_2026-06.md`
- Pipeline reliability: 5 runs, 12 avg finalists/night
- Signal performance: 20 resolved, 0.96R avg, 10% win rate
- Best strategy: pre_market_favourable (6.49R)

### 7. ✅ JS validator passes after dashboard changes
```bash
node -e 'const fs=require("fs"); let html=fs.readFileSync("/root/fund-system/public/system2-terminal.html","utf8"); let m=html.match(/<script>([\s\S]*?)<\/script>/); new Function(m[1]); console.log("OK");'
```
**Result:** `JS syntax OK`

---

## Architecture

```
nightly_learning.py (23:30 UTC)
  ├── compute_attribution()
  ├── compute_council_calibration()
  ├── generate_council_suggestions()
  ├── generate_postmortem()
  └── intelligence_engine.run_all()
        ├── track_shadow_performance()   → FMP 5d/10d returns for rejections
        ├── analyse_stage_funnel()       → pipeline run metadata
        ├── analyse_source_value()       → source attribution
        └── analyse_what_if()            → strategy simulation
              ↓
        data/intelligence_report.json
              ↓
    GET /api/system2/intelligence
              ↓
    Dashboard Intelligence tab (4 panels)
```

---

## Key Findings from First Run

| Area | Finding |
|------|---------|
| **Shadow Portfolio** | Overall avg 5d return: +0.31% (32% went up). 2 gates effective, 3 flagged for review |
| **Stage Funnel** | 80.2% pass Stage 1, 5.5% pass Stage 2, 36.4% become finalists |
| **Source Value** | `pre_market_gap_checked` adds +3.93R lift vs unchecked |
| **What-If** | `pre_market_favourable` strategy: 6.49R avg (100% win rate, 9 trades) |

---

## Notes

- Shadow portfolio uses existing `shadow_return_cache.json` (backward compatible with legacy float format)
- Intelligence report saved to `data/intelligence_report.json` after every nightly run
- Monthly markdown reports saved to `reports/monthly_YYYY-MM.md`
- Telegram alert sent on 1st of month when monthly report generated
- Dashboard backup: `system2-terminal.html.bak.20260610_161549`
