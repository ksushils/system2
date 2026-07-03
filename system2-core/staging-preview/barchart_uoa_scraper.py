#!/usr/bin/env python3
"""
Barchart Unusual Options Activity scraper for System 2.

Free public page: https://www.barchart.com/options/unusual-activity/stocks
No API key is required.  The page renders its table via JavaScript, but the
underlying API endpoint and query parameters are exposed in the
<barchart-datatable data-api-config="..."> tag.  This script:

1. Hits the public page with requests + BeautifulSoup to warm the session
   and extract the API configuration.
2. Calls the internal proxy API using the XSRF token set by the page.
3. Filters the top ~30 rows for bullish call candidates.
4. Writes the result to /root/system2-core/data/barchart_uoa.json.

Cron schedule (Mon-Fri @ 00:15 UTC):
    15 0 * * 1-5 /root/system2-core/.venv/bin/python /root/system2-core/staging-preview/barchart_uoa_scraper.py >> /root/system2-core/logs/barchart_uoa.log 2>&1

Fail-open behaviour: any exception results in an output file with an empty
candidates list and metadata.barchart_scrape_failed = true so the pipeline
continues without aborting.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("barchart_uoa")

PAGE_URL = "https://www.barchart.com/options/unusual-activity/stocks"
API_URL = "https://www.barchart.com/proxies/core-api/v1/options/get"
OUTPUT_PATH = "/root/system2-core/data/barchart_uoa.json"
LIMIT = 30
TIMEOUT = 10

EXCLUDED_SYMBOLS = {"SPX", "NDX", "VIX"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def parse_number(value):
    """Parse a Barchart numeric string (commas, percentages) into float."""
    if value is None:
        return None
    s = str(value).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "N/A", "n/a"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_expiration(value):
    """Convert Barchart expiration strings such as '06/12/26' to 'YYYY-MM-DD'."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", value)
    if m:
        mo, dy, yr = m.groups()
        yr = int(yr)
        if yr < 100:
            yr += 2000
        try:
            return datetime(yr, int(mo), int(dy)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


def discover_api_config(session, headers):
    """
    Fetch the public UOA page and extract the datatable's API configuration.
    This also warms the session cookies required for the proxied API call.
    """
    logger.info("Fetching Barchart UOA page: %s", PAGE_URL)
    r = session.get(PAGE_URL, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    logger.info("Page fetched (status=%s, len=%s)", r.status_code, len(r.text))

    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("barchart-datatable")
    if not table:
        raise RuntimeError("Could not find <barchart-datatable> tag; page structure may have changed.")

    raw_config = table.get("data-api-config")
    if not raw_config:
        raise RuntimeError("No data-api-config attribute on <barchart-datatable>.")

    config = json.loads(raw_config)
    logger.info(
        "Discovered API config: method=%s orderBy=%s limit=%s",
        config.get("api", {}).get("method"),
        config.get("api", {}).get("orderBy"),
        config.get("api", {}).get("limit"),
    )
    return config


def fetch_rows(session, xsrf_token):
    """Call the Barchart proxy API and return the raw row list."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    fields = (
        "symbol,baseSymbol,baseLastPrice,baseSymbolType,expirationDate,"
        "daysToExpiration,symbolType,strikePrice,volume,openInterest,"
        "volumeOpenInterestRatio,weightedImpliedVolatility,tradeTime"
    )

    params = {
        "fields": fields,
        "orderBy": "volumeOpenInterestRatio",
        "orderDir": "desc",
        "baseSymbolTypes": "stock",
        "between(volumeOpenInterestRatio,1.24,)": "",
        "between(lastPrice,.10,)": "",
        f"between(tradeTime,{today},{tomorrow})": "",
        "between(volume,500,)": "",
        "between(openInterest,100,)": "",
        "in(exchange,(AMEX,NYSE,NASDAQ,INDEX-CBOE))": "",
        "limit": LIMIT,
        "meta": "field.shortName,field.type",
    }

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": PAGE_URL,
        "X-XSRF-TOKEN": unquote(xsrf_token) if xsrf_token else "",
    }

    logger.info("Calling Barchart UOA API with tradeTime %s -> %s", today, tomorrow)
    resp = session.get(API_URL, params=params, headers=headers, timeout=TIMEOUT)
    logger.info("API response status=%s len=%s", resp.status_code, len(resp.text))
    resp.raise_for_status()

    payload = resp.json()
    rows = payload.get("data")
    if rows is None:
        raise RuntimeError(f"Unexpected API response keys: {list(payload.keys())}")

    logger.info("API returned %d row(s)", len(rows))
    return rows


def transform(rows):
    """Apply the System 2 filter to the raw API rows."""
    candidates = []
    for row in rows:
        option_type = str(row.get("symbolType") or "").strip().lower()
        if option_type != "call":
            continue

        vol_oi = parse_number(row.get("volumeOpenInterestRatio"))
        if vol_oi is None or vol_oi <= 2.0:
            continue

        underlying = parse_number(row.get("baseLastPrice"))
        if underlying is None or underlying <= 10.0:
            continue

        symbol = str(row.get("baseSymbol") or "").strip().upper()
        if not symbol or symbol in EXCLUDED_SYMBOLS:
            continue

        strike = parse_number(row.get("strikePrice"))
        iv = parse_number(row.get("weightedImpliedVolatility"))
        if iv is not None:
            iv = iv / 100.0
        expiration = parse_expiration(row.get("expirationDate"))

        candidates.append({
            "symbol": symbol,
            "option_type": option_type,
            "vol_oi_ratio": round(vol_oi, 2),
            "implied_volatility": round(iv, 4) if iv is not None else None,
            "expiration": expiration,
            "strike": strike,
            "underlying_price": round(underlying, 2),
            "source": "barchart_uoa",
        })

    logger.info("Filtered to %d candidate(s)", len(candidates))
    return candidates


def write_output(candidates, failed=False):
    """Write the final JSON payload, creating parent dirs if necessary."""
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    output = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "candidates": candidates,
    }
    if failed:
        output["metadata"] = {"barchart_scrape_failed": True}
    else:
        output["metadata"] = {
            "barchart_scrape_failed": False,
            "count": len(candidates),
        }

    with open(OUTPUT_PATH, "w") as fh:
        json.dump(output, fh, indent=2)

    logger.info("Wrote output to %s (%d candidates)", OUTPUT_PATH, len(candidates))


def main():
    try:
        session = requests.Session()
        page_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        discover_api_config(session, page_headers)

        xsrf = session.cookies.get("XSRF-TOKEN", "")
        if not xsrf:
            logger.warning("XSRF-TOKEN cookie is missing; API call may be blocked.")

        rows = fetch_rows(session, xsrf)
        candidates = transform(rows)
        write_output(candidates, failed=False)
    except Exception as exc:
        logger.exception("Barchart UOA scrape failed: %s", exc)
        write_output([], failed=True)
        logger.error("barchart_scrape_failed=true")


if __name__ == "__main__":
    main()
