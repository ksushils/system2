# System 2 — Four Free Data Sources Expansion

**Date:** 2026-06-12  
**Mode:** Paper only  
**Pipeline status:** ✅ SUCCESS (`logs/phase_b_core_20260612T200755Z.json`)

## Sources added

| Source | Role | File | Status |
|--------|------|------|--------|
| ApeWisdom | Reddit-style retail sentiment; spikes feed universe expansion + social enrichment | `social_scraper.py` | ✅ 200 stocks, 94 spiking |
| Finviz New 52-Week Highs + Unusual Volume | Free pre-market technical bypass candidates | `finviz_scraper.py` | ✅ 50 new highs, 17 unusual volume |
| Market Chameleon UOA | Secondary unusual-options confirmation (merged with Barchart) | `barchart_uoa_scraper.py` | ⚠️  Blocked by VPS/hosting detection; integration in place, returns 0 |
| Options Universe Expander | Adds high-conviction options-flow + ApeWisdom tickers before B1 | `options_universe_expander.py` | ✅ 3 ApeWisdom additions queued |

## Files changed / created

- `social_scraper.py` — ApeWisdom fetch + snapshot + discovery-candidate enrichment
- `finviz_scraper.py` — new file, Playwright-based Finviz screener scraper
- `catalyst_discovery.py` — loads `data/finviz_signals.json` as bypass candidates
- `barchart_uoa_scraper.py` — MarketChameleon fetch + multi-source merge
- `options_universe_expander.py` — new pipeline pre-step
- `run_phase_b_core_baseline.py` — added "Options Universe Expansion" step before B1
- `universe_builder.py` — merges expansion tickers into `universe.json`

Backups: `*.bak.20260612_200559` / `*.bak.20260612_194823` in `/root/system2-core/`.

## Cron schedule

```text
# ApeWisdom + social discovery
40 0 * * 1-5 cd /root/system2-core && .venv/bin/python social_scraper.py >> /root/system2-core/logs/social_scraper.log 2>&1

# Finviz new highs + unusual volume
55 0 * * 1-5 cd /root/system2-core && .venv/bin/python finviz_scraper.py >> /root/system2-core/logs/finviz_scraper.log 2>&1
```

Existing Barchart UOA cron at `15 0 * * 1-5` and the main pipeline at `15 2 * * 2-6` remain unchanged.

## Validation

### Individual smoke tests

- `python3 finviz_scraper.py` → `data/finviz_signals.json` (66 combined tickers)
- `python3 social_scraper.py` → `data/apewisdom_sentiment.json` (200 tracked, 94 spiking)
- `python3 options_universe_expander.py` → `data/options_universe_expansion.json` (3 additions)
- `python3 -m py_compile` passed for all modified/new Python files

### Full pipeline run

```text
log: logs/phase_b_core_20260612T200755Z.json
status: SUCCESS
finalists: 17
alignment: OK
```

Key step runtimes:

- B2 cheap filter: 518 s
- B3 technical score: 448 s
- Stage5 Chronos/Kronos: 263 s
- Stage6 council v2: 489 s
- Shadow portfolio: 117 s

### First-run note

The first run (`logs/phase_b_core_20260612T192702Z.json`) hit the existing stage-alignment guard because stale `stage6_council_enriched.json` from the previous run mismatched the new `stage2` symbol set. After removing stale `stage6_*` / `stage7_*` / `stage2_confluence_ranked_top40.json` files, the rerun completed cleanly.

## Known issues / watch items

- **MarketChameleon** returns an access-denied page from the VPS (managed-hosting detection). The merge logic is wired; if the block is lifted or a proxy is added it will start contributing multi-source UOA flags with no code changes.
- **Barchart UOA API** returned 0 rows for this off-market test window; this is pre-existing behavior and unrelated to the source expansion.
- Finviz relies on **Playwright/Chromium**. The browser is installed and the headless scrape works; cron should be monitored for first few runs.
