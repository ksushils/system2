#!/usr/bin/env python3
"""Finviz free screener signals for System 2.

Sources:
  - New 52-week highs
  - Unusual volume

No API key required. Runs before the nightly pipeline (cron: 55 0 * * 1-5).

Note: Finviz renders its screener table with JavaScript, so this scraper uses
Playwright to obtain the data. A lightweight requests fallback is included for
environments without Playwright, but it will normally return an empty list.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "data" / "finviz_signals.json"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

FINVIZ_SCREENS = {
    "new_highs": {
        "url": "https://finviz.com/screener.ashx?v=111&s=ta_newhigh&f=cap_smallover,sh_avgvol_o500",
        "label": "New 52-Week Highs",
    },
    "unusual_vol": {
        "url": "https://finviz.com/screener.ashx?v=111&s=ta_unusualvolume&f=cap_smallover",
        "label": "Unusual Volume",
    },
}


def _parse_static_html(html: str) -> list[str]:
    """Best-effort parser for non-JavaScript HTML (usually empty for Finviz)."""
    tickers: list[str] = []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for table in soup.find_all("table", class_=lambda c: c and "screener" in c.lower()):
            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    ticker = cells[1].get_text(strip=True).upper()
                    if ticker and ticker not in tickers:
                        tickers.append(ticker)
    except Exception:
        pass
    return tickers


def _fetch_with_requests(url: str) -> list[str]:
    """Lightweight requests fallback; Finviz JS tables are usually not populated."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=20,
        )
        if resp.status_code == 200:
            return _parse_static_html(resp.text)
    except Exception as e:
        print(f"Finviz requests fallback error: {e}")
    return []


def _fetch_with_playwright(url: str, max_pages: int = 3) -> list[str]:
    """Render the Finviz screener with Playwright and extract tickers."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    tickers: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            for page_num in range(max_pages):
                start_row = page_num * 20 + 1
                page_url = f"{url}&r={start_row}"
                page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                # Give the JS table a moment to populate.
                time.sleep(2.5)

                rows = page.query_selector_all("table.screener_table tr")
                if not rows:
                    break

                added_this_page = 0
                for row in rows[1:]:
                    cells = row.query_selector_all("td")
                    if len(cells) >= 2:
                        ticker = cells[1].inner_text().strip().upper()
                        # Basic ticker sanity filter.
                        if ticker and ticker.replace(".", "").replace("-", "").isalnum() and ticker not in tickers:
                            tickers.append(ticker)
                            added_this_page += 1

                print(f"  Finviz page {page_num + 1} (r={start_row}): {added_this_page} tickers")
                if added_this_page == 0:
                    break
        finally:
            browser.close()

    return tickers


def fetch_finviz_screen(key: str) -> list[str]:
    """Fetch tickers for a given Finviz screen key."""
    config = FINVIZ_SCREENS[key]
    url = config["url"]
    label = config["label"]
    print(f"Fetching Finviz {label} ...")

    tickers = _fetch_with_playwright(url)
    if not tickers:
        print(f"  Playwright returned no data for {label}; trying requests fallback.")
        tickers = _fetch_with_requests(url)

    print(f"Finviz {label}: {len(tickers)} stocks")
    return tickers[:50]


def fetch_finviz_new_highs() -> dict[str, Any]:
    """Fetch Finviz new 52-week highs and unusual volume screens."""
    return {
        "new_highs": fetch_finviz_screen("new_highs"),
        "unusual_vol": fetch_finviz_screen("unusual_vol"),
    }


def run() -> None:
    data = fetch_finviz_new_highs()

    all_tickers = list(dict.fromkeys(data["new_highs"] + data["unusual_vol"]))

    output = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "finviz_screener",
        "new_highs": data["new_highs"],
        "unusual_vol": data["unusual_vol"],
        "combined_count": len(all_tickers),
        "all_tickers": all_tickers,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Finviz: {len(all_tickers)} total tickers -> {OUTPUT_PATH}")


if __name__ == "__main__":
    run()
