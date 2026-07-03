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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import requests
try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:
    BeautifulSoup = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("barchart_uoa")

ROOT = Path(__file__).resolve().parent
PAGE_URL = "https://www.barchart.com/options/unusual-activity/stocks"
API_URL = "https://www.barchart.com/proxies/core-api/v1/options/get"
OUTPUT_PATH = "/root/system2-core/data/barchart_uoa.json"
FINALIST_OPTIONS_PATH = ROOT / "data" / "finalist_options.json"
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


def _contract_number(row, key):
    raw = row.get("raw", row) if isinstance(row, dict) else {}
    return parse_number(raw.get(key, row.get(key)))


def build_options_structure(results):
    """Compute max pain and OI walls from Barchart option contract rows."""
    contracts = []
    today = datetime.now(timezone.utc).date()

    for row in results:
        raw = row.get("raw", row)
        opt_type = str(raw.get("symbolType") or row.get("symbolType") or "").lower()
        if "call" not in opt_type and "put" not in opt_type:
            continue

        strike = _contract_number(row, "strikePrice")
        expiry = parse_expiration(raw.get("expirationDate") or row.get("expirationDate"))
        oi = _contract_number(row, "openInterest") or 0
        volume = _contract_number(row, "volume") or 0
        if strike is None or not expiry:
            continue

        contracts.append({
            "type": "call" if "call" in opt_type else "put",
            "strike": float(strike),
            "expiry": expiry,
            "open_interest": float(oi),
            "volume": float(volume),
        })

    if not contracts:
        return {}

    expiries = sorted({c["expiry"] for c in contracts})
    future_expiries = []
    for exp in expiries:
        try:
            if datetime.strptime(exp, "%Y-%m-%d").date() >= today:
                future_expiries.append(exp)
        except ValueError:
            pass
    expiry = (future_expiries or expiries)[0]
    expiry_contracts = [c for c in contracts if c["expiry"] == expiry]
    if not expiry_contracts:
        return {}

    strikes = sorted({c["strike"] for c in expiry_contracts})
    payouts = []
    for test_strike in strikes:
        payout = 0.0
        for c in expiry_contracts:
            if c["type"] == "call":
                payout += c["open_interest"] * max(test_strike - c["strike"], 0)
            else:
                payout += c["open_interest"] * max(c["strike"] - test_strike, 0)
        payouts.append((payout, test_strike))
    max_pain = min(payouts)[1] if payouts else None

    def wall_rows(kind):
        by_strike = {}
        for c in expiry_contracts:
            if c["type"] != kind:
                continue
            bucket = by_strike.setdefault(c["strike"], {"strike": c["strike"], "oi": 0.0, "volume": 0.0})
            bucket["oi"] += c["open_interest"]
            bucket["volume"] += c["volume"]
        rows = sorted(by_strike.values(), key=lambda x: (x["oi"], x["volume"]), reverse=True)[:5]
        return [
            {
                "strike": round(r["strike"], 2),
                "oi": int(r["oi"]),
                "volume": int(r["volume"]),
            }
            for r in rows
        ]

    return {
        "max_pain": round(max_pain, 2) if max_pain is not None else None,
        "max_pain_expiry": expiry,
        "call_walls": wall_rows("call"),
        "put_walls": wall_rows("put"),
        "contracts_structured": len(expiry_contracts),
    }


def discover_api_config(session, headers):
    """
    Fetch the public UOA page and extract the datatable's API configuration.
    This also warms the session cookies required for the proxied API call.
    """
    logger.info("Fetching Barchart UOA page: %s", PAGE_URL)
    r = session.get(PAGE_URL, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    logger.info("Page fetched (status=%s, len=%s)", r.status_code, len(r.text))
    raw_config = None
    if BeautifulSoup is not None:
        soup = BeautifulSoup(r.text, "lxml")
        table = soup.find("barchart-datatable")
        raw_config = table.get("data-api-config") if table else None
    else:
        match = re.search(r'data-api-config="([^"]+)"', r.text)
        raw_config = unquote(match.group(1)) if match else None
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
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

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
        f"between(tradeTime,{yesterday},{today})": "",
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

    logger.info("Calling Barchart UOA API with tradeTime %s -> %s", yesterday, today)
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
    multi_source = sum(1 for c in candidates if c.get("multi_source_uoa"))
    output = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": candidates,
        "count": len(candidates),
    }
    output["metadata"] = {
        "barchart_scrape_failed": bool(failed),
        "count": len(candidates),
        "multi_source_uoa_count": multi_source,
    }
    output["multi_source_uoa_count"] = multi_source

    with open(OUTPUT_PATH, "w") as fh:
        json.dump(output, fh, indent=2)

    logger.info("Wrote output to %s (%d candidates, %d multi-source)", OUTPUT_PATH, len(candidates), multi_source)



def fetch_market_chameleon_uoa():
    """Market Chameleon unusual options volume report. Free, no auth."""
    try:
        resp = requests.get(
            "https://marketchameleon.com/Reports/UnusualOptionVolumeReport",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows)",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"MarketChameleon: HTTP {resp.status_code}")
            return []

        if BeautifulSoup is None:
            print("MarketChameleon: BeautifulSoup not available")
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table", {"class": lambda x: x and "report" in x.lower()}) or soup.find("table")
        if not table:
            print("MarketChameleon: no table found")
            return []

        results = []
        rows = table.find_all("tr")[1:]  # skip header
        for row in rows[:30]:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            try:
                ticker = cells[0].get_text().strip().upper()
                if not ticker or len(ticker) > 5 or ticker in EXCLUDED_SYMBOLS:
                    continue

                # Try to parse vol/OI if present in later cells
                vol_oi = None
                for cell in cells[2:6]:
                    txt = cell.get_text().strip().replace(",", "").replace("x", "")
                    try:
                        val = float(txt)
                        if val > 1:
                            vol_oi = val
                            break
                    except Exception:
                        continue

                results.append({
                    "symbol": ticker,
                    "option_type": "call",
                    "vol_oi_ratio": round(vol_oi, 2) if vol_oi else 0.0,
                    "implied_volatility": None,
                    "expiration": None,
                    "strike": None,
                    "underlying_price": None,
                    "source": "marketchameleon",
                })
            except Exception:
                continue

        print(f"MarketChameleon: {len(results)} stocks")
        return results

    except Exception as e:
        print(f"MarketChameleon error: {e}")
        return []


def load_finalists():
    """Load current finalist tickers for the per-ticker options pass."""
    for path in [
        ROOT / "stage2_confluence_ranked_top40.json",
        ROOT / "stage6_council_enriched.json",
        ROOT / "stage7_clustered_survivors.json",
    ]:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ideas = data if isinstance(data, list) else data.get("ideas", data.get("finalists", []))
            finalists = []
            seen = set()
            for idea in ideas:
                if not isinstance(idea, dict):
                    continue
                ticker = str(idea.get("ticker") or idea.get("symbol") or "").strip().upper()
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    finalists.append(ticker)
            if finalists:
                return finalists
        except Exception as exc:
            logger.warning("Could not load finalists from %s: %s", path, exc)
    return []


def _nearby_number_from_label(soup, label):
    if BeautifulSoup is None:
        return None
    for elem in soup.find_all(string=lambda t: t and label.lower() in t.lower()):
        parent = elem.parent
        candidates = []
        if parent:
            sibling = parent.find_next_sibling()
            if sibling:
                candidates.append(sibling.get_text(" ", strip=True))
            grand = parent.parent
            if grand:
                candidates.append(grand.get_text(" ", strip=True))
        for text in candidates:
            match = re.search(r"(-?\d+(?:\.\d+)?)\s*%?", text.replace(",", ""))
            if match:
                try:
                    return float(match.group(1))
                except Exception:
                    pass
    return None


def scrape_ticker_options(ticker):
    """
    Use Barchart's quote endpoint instead of scraping static HTML.
    More reliable when the options page does not expose summary labels.
    """
    fields = (
        "symbol,baseSymbol,baseLastPrice,symbolType,strikePrice,expirationDate,"
        "daysToExpiration,volume,openInterest,impliedVolatility,lastPrice"
    )
    url = (
        "https://www.barchart.com/proxies/core-api/v1/options/get?"
        f"baseSymbol={ticker}&"
        f"fields={fields}&"
        "orderBy=volume&orderDir=desc&limit=250&raw=1"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0)",
        "Accept": "application/json",
        "Referer": f"https://www.barchart.com/stocks/quotes/{ticker}/options",
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRF-TOKEN": "",
    }

    try:
        session = requests.Session()
        main_page = session.get(
            f"https://www.barchart.com/stocks/quotes/{ticker}/options",
            headers={"User-Agent": headers["User-Agent"]},
            timeout=10,
        )
        if main_page.status_code != 200:
            logger.warning("Barchart options warmup %s HTTP %s", ticker, main_page.status_code)

        xsrf = session.cookies.get("XSRF-TOKEN", "")
        if xsrf:
            headers["X-XSRF-TOKEN"] = unquote(xsrf)

        resp = session.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("data", [])
            if results:
                call_vol = 0
                put_vol = 0
                iv_values = []
                structure = build_options_structure(results)
                underlying_prices = []
                for row in results:
                    q = row.get("raw", row)
                    opt_type = str(q.get("symbolType") or row.get("symbolType") or "").lower()
                    volume = q.get("volume", row.get("volume", 0)) or 0
                    try:
                        volume = float(str(volume).replace(",", ""))
                    except Exception:
                        volume = 0
                    if "call" in opt_type:
                        call_vol += volume
                    elif "put" in opt_type:
                        put_vol += volume

                    iv_raw = q.get("impliedVolatility", row.get("impliedVolatility"))
                    try:
                        if iv_raw not in (None, "", "N/A"):
                            iv_values.append(float(str(iv_raw).replace("%", "")))
                    except Exception:
                        pass

                    base_price = parse_number(q.get("baseLastPrice") or row.get("baseLastPrice"))
                    if base_price:
                        underlying_prices.append(base_price)

                pcr = None

                if pcr is None and call_vol > 0:
                    pcr = put_vol / call_vol

                iv_pct = None
                iv = round(sum(iv_values) / len(iv_values), 4) if iv_values else None
                underlying_price = round(sum(underlying_prices) / len(underlying_prices), 2) if underlying_prices else None
                expected_move = None
                if underlying_price and iv and structure.get("max_pain_expiry"):
                    try:
                        exp_dt = datetime.strptime(structure["max_pain_expiry"], "%Y-%m-%d").date()
                        dte = max((exp_dt - datetime.now(timezone.utc).date()).days, 1)
                        iv_decimal = iv / 100.0 if iv > 3 else iv
                        expected_move = round(underlying_price * iv_decimal * ((dte / 365.0) ** 0.5), 2)
                    except Exception:
                        expected_move = None

                if pcr is not None:
                    pcr = float(pcr)
                    if pcr < 0.5:
                        verdict = "BULLISH_CONFIRM"
                        signal = f"Put/call {pcr:.2f} - heavy call bias"
                    elif pcr < 0.7:
                        verdict = "CONFIRM"
                        signal = f"Put/call {pcr:.2f} - call favoured"
                    elif pcr > 1.3:
                        verdict = "CAUTION"
                        signal = f"Put/call {pcr:.2f} - put heavy"
                    else:
                        verdict = "NEUTRAL"
                        signal = f"Put/call {pcr:.2f} - balanced"
                else:
                    verdict = "NO_DATA"
                    signal = "No options data"

                return {
                    "ticker": ticker,
                    "source": "barchart_api",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "underlying_price": underlying_price,
                    "put_call_ratio": round(pcr, 3) if pcr is not None else None,
                    "call_volume": int(float(call_vol or 0)),
                    "put_volume": int(float(put_vol or 0)),
                    "iv_percentile": iv_pct,
                    "iv_rank": iv_pct,
                    "implied_volatility": iv,
                    "expected_move": expected_move,
                    **structure,
                    "contracts_sampled": len(results),
                    "options_verdict": verdict,
                    "options_signal": signal,
                }

        logger.warning("Barchart quote API %s HTTP %s body=%s", ticker, resp.status_code, resp.text[:160])
    except Exception as exc:
        print(f"Barchart API {ticker}: {exc}")

    return {
        "ticker": ticker,
        "source": "barchart_api",
        "options_verdict": "NO_DATA",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

def scrape_finalist_options():
    """Second pass: per-ticker options scrape for current finalists."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    finalist_options = {}
    finalists = load_finalists()

    if finalists:
        logger.info("Scraping options for %d finalists", len(finalists[:20]))
        for ticker in finalists[:20]:
            result = scrape_ticker_options(ticker)
            if result:
                finalist_options[ticker] = result
            time.sleep(2)
        logger.info("Got options data for %d finalists", len(finalist_options))
    else:
        logger.warning("No finalists found for per-ticker options scrape")

    FINALIST_OPTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FINALIST_OPTIONS_PATH.write_text(
        json.dumps(
            {
                "date": today,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "barchart_per_ticker",
                "count": len(finalist_options),
                "tickers": finalist_options,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Wrote finalist options to %s (%d tickers)", FINALIST_OPTIONS_PATH, len(finalist_options))


def main():
    bc_candidates = []
    barchart_failed = False
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
        bc_candidates = transform(rows)
    except Exception as exc:
        barchart_failed = True
        logger.exception("Barchart UOA scrape failed; trying secondary source: %s", exc)

    # Market Chameleon is always attempted so a blocked Barchart page does not
    # leave the UOA feed empty.
    mc_candidates = fetch_market_chameleon_uoa()
    by_symbol = {}
    for c in bc_candidates:
        by_symbol[c["symbol"]] = {**c, "multi_source_uoa": False}
    for c in mc_candidates:
        sym = c["symbol"]
        if sym in by_symbol:
            by_symbol[sym]["multi_source_uoa"] = True
            by_symbol[sym]["sources"] = ["barchart_uoa", "marketchameleon"]
            if c.get("vol_oi_ratio", 0) > by_symbol[sym].get("vol_oi_ratio", 0):
                by_symbol[sym]["vol_oi_ratio"] = c["vol_oi_ratio"]
        else:
            by_symbol[sym] = {**c, "multi_source_uoa": False}
    candidates = list(by_symbol.values())
    candidates.sort(key=lambda x: x.get("vol_oi_ratio", 0), reverse=True)

    write_output(candidates, failed=barchart_failed and not candidates)
    if barchart_failed and not candidates:
        logger.error("barchart_scrape_failed=true")
    scrape_finalist_options()


if __name__ == "__main__":
    main()
