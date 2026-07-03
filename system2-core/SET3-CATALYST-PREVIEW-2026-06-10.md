# Set 3 Catalyst Driven Universe — Preview Report
**Date:** 2026-06-10  
**Status:** STAGED / PREVIEW — NOT wired into pipeline  
**Gate condition:** Set 2 must reach ≥5 resolved ideas before Set 3 activation

---

## Summary

Set 3 is a new **catalyst-driven discovery stream** that sources trade ideas from news, analyst actions, earnings surprises, insider buying, and corporate events via FMP stable endpoints. It runs **independently** from Set 1 (technical momentum) and Set 2 (options flow).

### Pipeline Flow

```
catalyst_discovery.py  →  set3_scorer.py  →  [STAGED: merge_sets.py]
     (FMP fetch)           (quote enrich)       (NOT auto-called)
```

---

## Components Built

### 1. `catalyst_discovery.py` (pre-existing, audited)
- **Source:** FMP stable endpoints
- **Endpoints used:**
  - `stable/earnings-calendar` — earnings surprises (works, 3+ records)
  - `stable/price-target-latest-news` — analyst PT changes (works, 149 records)
  - `stable/insider-trading/latest` — insider purchases (works, 1 record)
  - `stable/news/stock-latest` + `stable/news/press-releases-latest` — corporate/FDA news (works, sparse)
- **Output:** `catalyst_candidates.json` (deduped, scored)
- **Metadata:** `catalyst_discovery_metadata.json`

### 2. `set3_scorer.py` (NEW)
- **Input:** `catalyst_candidates.json`
- **Enrichment:** FMP `stable/batch-quote` (price, volume, 50MA, 200MA, market cap)
- **Directional bias engine:**
  - Parses catalyst text for bullish/bearish keywords with weighted scoring
  - Upgrade/downgrade weighted ±3, PT raised/lowered ±2, beat/miss ±2
  - Sub-type bonuses for earnings/insider/FDA context
  - Returns `BULLISH` / `BEARISH` / `NEUTRAL` with confidence score
- **Hard rejects:**
  - Price < $10 or > $2000
  - Volume < 200K
  - **Bearish directional bias** (auto-reject)
- **Scoring formula:**
  - Core: catalyst_score mapped to 0–40 points
  - Technical: +12 (price > 50MA), +8 (price > 200MA), +4–8 (positive change)
  - Multi-catalyst: +10 (≥2 types), +5 (1 type)
  - Bias confidence: up to +10
  - Risk: +10 (earnings today), +8 (below 50MA), +10 (< -2% change)
  - Trade quality = max(0, min(100, core × 0.7 − risk × 0.3))
- **Output:** `data/set3_scored.json` (same schema as set2_scored.json)
- **Metadata:** `data/set3_scored_metadata.json`

### 3. `merge_sets.py` (UPDATED)
- **Now supports 3-way merge:** Set 1 + Set 2 + Set 3
- **Multi-set overlap tracking:**
  - `multi_12_count` — overlap between Set 1 and Set 2
  - `multi_13_count` — overlap between Set 1 and Set 3
  - `multi_23_count` — overlap between Set 2 and Set 3
  - `multi_123_count` — triple overlap
- **Confluence bonus:** +8 for any 2-set overlap, +12 for triple overlap
- **Field merging:** Set 2 options fields and Set 3 catalyst fields merged into base record
- **Backward compatible:** Works with only Set 1, Set 1+2, or all three sets

### 4. Dashboard Updates (`system2-terminal.html`)
- **`set3Section(r)`** — collapsible "🔥 CATALYST SETUP" panel showing:
  - Directional bias (color-coded: green bullish, red bearish)
  - Catalyst score, trade quality, sub-types
  - Price target, publisher/analyst firm, summary
- **`generateNarrative(r)`** — now prefixes with catalyst type for Set 3 ideas
- **`generateBearCase(r)`** — adds bearish catalyst and high risk warnings
- Trade cards render Set 3 section automatically when `set === 3` or `multi_set_idea === true`

---

## Preview Run Results (2026-06-10)

### Catalyst Discovery
| Source | Raw Count |
|--------|----------|
| Earnings surprises | 3 |
| Analyst PT changes | 149 |
| Insider purchases | 1 |
| Corporate events | 2 |
| FDA/biotech | 2 |
| **Deduped candidates** | **50** |

### Set 3 Scorer
| Metric | Value |
|--------|-------|
| Input candidates | 50 |
| Batch quotes fetched | 50 (100%) |
| Earnings calendar hits | 1 |
| **OK (passed filters)** | **37** |
| Rejected | 13 |
| BULLISH bias | 43 |
| BEARISH bias | 6 |
| NEUTRAL bias | 1 |

### Rejection Breakdown
| Reason | Count |
|--------|-------|
| Bearish catalyst | 6 |
| Volume too low | 7 |
| Price out of range | 1 |

### Top Scored Candidates
| Symbol | Bias | Trade Quality | Core | Risk | Price | Catalyst |
|--------|------|---------------|------|------|-------|----------|
| SJM | BULLISH | 50.4 | 72 | 0 | $116.04 | PT raised to $130 |
| TNGX | BULLISH | 49.5 | 75 | 10 | $30.62 | PT raised to $66 |
| DDOG | BULLISH | 47.6 | 68 | 0 | $230.50 | PT raised to $260 |
| CASY | BULLISH | 47.6 | 68 | 0 | $880.05 | EPS beat +32% |
| SAIL | BULLISH | 46.7 | 71 | 10 | $14.995 | PT raised + earnings |

### Preview Merge (vs Set 1)
| Metric | Value |
|--------|-------|
| Set 1 ideas | 39 |
| Set 3 ideas | 37 |
| Overlap | 1 (MAC) |

---

## FMP API Reality Check

| Endpoint | Status | Notes |
|----------|--------|-------|
| `stable/batch-quote` | ✅ Works | Price, volume, 50MA, 200MA, market cap |
| `stable/earnings-calendar` | ✅ Works | 4000 records, 2972 with data |
| `stable/price-target-latest-news` | ✅ Works | 149 records in 72h lookback |
| `stable/insider-trading/latest` | ✅ Works | Sparse (1 record) |
| `stable/news/stock-latest` | ✅ Works | Sparse |
| `stable/profile` | ❌ Empty | Returns 0 records |
| `stable/upgrades-downgrades` | ❌ Empty | Returns 0 records |
| `stable/earnings-surprises` | ❌ Empty | Returns 0 records |
| `stable/stock-news` | ❌ Empty | Returns 0 records |

**Verdict:** Current FMP tier supports enough endpoints for a viable Set 3, but data is sparse for some categories. The `price-target-latest-news` endpoint is the workhorse (149 records). Earnings calendar works well. Other endpoints (profile, upgrades-downgrades, stock-news) return empty on free tier.

---

## Activation Checklist

Set 3 **CANNOT** be wired into the production pipeline until:

- [ ] Set 2 reaches **≥5 resolved ideas** with measurable outcomes
- [ ] FMP key upgraded OR fallback data sources confirmed viable
- [ ] `run_phase_b_core_baseline.py` updated to include Set 3 steps
- [ ] `nightly_learning.py` updated to log Set 3 signal snapshots
- [ ] Intelligence Engine attribution updated for Set 3 source
- [ ] Paper-mode shadow tracking enabled for Set 3 candidates
- [ ] Council calibration includes Set 3 model performance (if council ever reviews Set 3)

---

## Files Modified / Created

| File | Action | Location |
|------|--------|----------|
| `set3_scorer.py` | **Created** | `/root/system2-core/` |
| `merge_sets.py` | **Updated** | `/root/system2-core/` |
| `system2-terminal.html` | **Updated** | `/root/fund-system/public/` |
| `run_set3_preview.sh` | **Created** | `/root/system2-core/` |
| `catalyst_discovery.py` | **Audited** (no changes) | `/root/system2-core/` |

---

## How to Run Preview

```bash
cd /root/system2-core
bash run_set3_preview.sh
```

Or step-by-step:
```bash
python3 catalyst_discovery.py --limit 50 --lookback-hours 72
python3 set3_scorer.py
# Merge is NOT run automatically — inspect data/set3_scored.json manually
```

---

## Notes

- **Paper mode only.** No live trading. All Set 3 signals are ride-along.
- The directional bias engine is heuristic-based and may require tuning as more data accumulates.
- Bearish catalysts are hard-rejected. Consider a "short candidate" sub-list in the future.
- Triple-overlap (Set 1 + 2 + 3) is rare but would receive +12 confluence bonus — highest in the system.
