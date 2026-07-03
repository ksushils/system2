# TipRanks UOA Scraper — Preview Report
**Date:** 2026-06-09  
**Status:** Preview / Ready for session cookie activation

---

## 1. FMP Congressional Endpoint Test Results

| Endpoint | URL | Result |
|----------|-----|--------|
| Senate Trading | `/api/v4/senate-trading` | ❌ Legacy Endpoint — subscription not valid |
| Senate Trading RSS | `/api/v4/senate-trading-rss-feed` | ❌ Legacy Endpoint — subscription not valid |
| House Disclosure | `/api/v4/house-disclosure` | ❌ Legacy Endpoint — subscription not valid |
| Congressional Trading | `/api/v4/congressional-trading` | ❌ Legacy Endpoint — subscription not valid |
| Insider Trading | `/api/v4/insider-trading` | ❌ Legacy Endpoint — subscription not valid |
| Governance | `/api/v4/governance` | ❌ Legacy Endpoint — subscription not valid |

**Root cause:** The FMP API key (`q8RajUBegYvlnJHIe366CBqk4dK2OKd7`) is on a tier that does **not** include congressional/insider trading data. FMP retired these endpoints for non-legacy subscribers after August 31, 2025.

**Congressional purchases found:** **0** — all endpoints blocked.

> **Note:** If congressional data is needed, options are:
> 1. Upgrade FMP subscription to a tier that includes political trading
> 2. Use an alternative source (e.g., Quiver Quantitative API, OpenSecrets bulk data)

---

## 2. TipRanks Scraper — Rows Extracted

| Metric | Value |
|--------|-------|
| **Raw rows extracted** | **0** |
| **Candidates passing filter** | **0** |
| **Scrape status** | `tipranks_scrape_failed: true` |

### Why 0 rows?
TipRanks serves a **Cloudflare security challenge** (`"Just a moment..."`) when accessing `https://www.tipranks.com/options/unusual-activity-stocks` without a valid Premium session cookie. Playwright (with and without `playwright-stealth`) is blocked by this challenge.

**Tested approaches:**
| Approach | Result |
|----------|--------|
| Plain Playwright (headless Chromium) | ❌ Cloudflare challenge |
| Playwright + realistic viewport/UA | ❌ Cloudflare challenge |
| Playwright + dummy session cookie | ❌ Cloudflare challenge |
| Playwright + `playwright-stealth` | ❌ Cloudflare challenge |
| Direct API calls (`/api/options/unusualActivity`) | ❌ Cloudflare challenge |

**Conclusion:** A **valid TipRanks Premium session cookie** is required to bypass Cloudflare and access the UOA table.

---

## 3. Scraper Architecture (Ready for Activation)

### File
`/root/system2-core/tipranks_uoa_scraper.py`

### What it does
1. Reads `TIPRANKS_SESSION_COOKIE` from `/root/system2-core/.env`
2. Launches Playwright (headless Chromium)
3. Injects the session cookie into the browser context
4. Navigates to `https://www.tipranks.com/options/unusual-activity-stocks`
5. Detects Cloudflare challenge / login wall (fail-safe)
6. Scrapes the full options table using multiple selector fallbacks:
   - `table tbody tr`
   - `[data-testid='unusual-activity-table'] tbody tr`
   - `[class*='table'] tbody tr`
   - `[role='row']`
7. Extracts: `symbol`, `contract_name`, `sentiment`, `expiration_date`, `option_type`, `strike`, `price`, `volume`, `open_interest`, `vol_oi_ratio`
8. Filters for **Set 2 candidates**:
   - `sentiment == "Bullish"`
   - `option_type == "Call"`
   - `vol_oi_ratio > 2.0`
   - `open_interest > 100`
9. Writes `/root/system2-core/data/tipranks_uoa.json`

### Output Schema
```json
{
  "scraped_at": "2026-06-09T10:06:13.761241+00:00",
  "source": "tipranks",
  "options_flow_source": "tipranks",
  "url": "https://www.tipranks.com/options/unusual-activity-stocks",
  "raw_rows_extracted": 0,
  "candidates_count": 0,
  "candidates": [],
  "tipranks_scrape_failed": true,
  "filters": {
    "sentiment": "Bullish",
    "option_type": "Call",
    "min_vol_oi_ratio": 2.0,
    "min_open_interest": 100
  }
}
```

### Fail-Safe Behavior
If **any** of these occur:
- Session cookie missing/invalid
- Cloudflare challenge detected
- Login wall detected
- Page load timeout
- Table not found
- Unexpected exception

The scraper:
- Logs `tipranks_scrape_failed=true`
- Writes empty candidates array
- **Does NOT crash the pipeline**
- Pipeline continues normally

### Cron Schedule
```
# TipRanks UOA scraper — runs before Barchart at 00:15 UTC
10 0 * * 1-5 /usr/bin/python3 /root/system2-core/tipranks_uoa_scraper.py >> /root/system2-core/logs/tipranks_cron.log 2>&1
```
Runs **00:10 UTC, Monday–Friday** (5 minutes before Barchart).

---

## 4. Dependencies Installed

| Package | Version | Method |
|---------|---------|--------|
| `playwright` | 1.60.0 | `pip install playwright --break-system-packages` |
| `playwright-stealth` | 2.0.3 | `pip install playwright-stealth --break-system-packages` |
| Chromium browser | 1223 | Already present in `/root/.cache/ms-playwright/` |

---

## 5. Next Steps to Activate

1. **Obtain a TipRanks Premium session cookie**
   - Log in to TipRanks Premium in a browser
   - Open DevTools → Application → Cookies → `www.tipranks.com`
   - Copy the value of the session cookie (often named `tr_session` or similar)
   - Paste it into `/root/system2-core/.env`:
     ```
     TIPRANKS_SESSION_COOKIE=your_session_cookie_here
     ```

2. **Test the scraper manually**
   ```bash
   python3 /root/system2-core/tipranks_uoa_scraper.py
   ```
   Then check:
   ```bash
   cat /root/system2-core/data/tipranks_uoa.json
   ```

3. **If successful**, wire into Set 2 pipeline:
   - Read `/root/system2-core/data/tipranks_uoa.json`
   - Merge candidates alongside Barchart + ImpliedOptions
   - Tag: `options_flow_source = "tipranks"`

4. **If still blocked by Cloudflare** even with a valid cookie:
   - Consider running the scraper from a residential IP
   - Or use a browser automation service (e.g., Bright Data, ScrapingBee)
   - Or switch to an alternative data source (e.g., Barchart UOA API, Unusual Whales)

---

## 6. Files Created / Modified

| File | Purpose |
|------|---------|
| `/root/system2-core/tipranks_uoa_scraper.py` | Scraper script |
| `/root/system2-core/data/tipranks_uoa.json` | Output (currently empty, fail-safe) |
| `/root/system2-core/logs/tipranks_scraper.log` | Scraper execution log |
| `/root/system2-core/.env` | Added `TIPRANKS_SESSION_COOKIE=` placeholder |
| Crontab | Added `10 0 * * 1-5` entry for scraper |

---

## Summary

| Question | Answer |
|----------|--------|
| FMP congressional purchases found? | **0** — endpoints legacy/subscription blocked |
| TipRanks rows extracted? | **0** — Cloudflare challenge without session cookie |
| Bullish call candidates? | **0** — no data to filter |
| Scraper ready? | **Yes** — needs valid `TIPRANKS_SESSION_COOKIE` to activate |
| Pipeline broken? | **No** — fail-safe writes empty output, pipeline continues |
