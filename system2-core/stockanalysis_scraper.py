#!/root/system2-core/.venv/bin/python3
"""
StockAnalysis scraper for System 2.

Reads Stage 1 survivors, scrapes per-ticker statistics from StockAnalysis,
scrapes the nightly most-shorted list, and persists JSON artifacts.

Designed to run on the VPS at:
    /root/system2-core/staging-preview/stockanalysis_scraper.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path("/root/system2-core")
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

STAGE1_PATH = BASE_DIR / "stage1_survivors.json"
OUTPUT_PATH = DATA_DIR / "stockanalysis_scores.json"
HIGH_SHORT_PATH = DATA_DIR / "high_short_universe.json"
ENV_PATH = BASE_DIR / ".env"

TIMEOUT_SECONDS = 10
DELAY_SECONDS = 1.5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def setup_logging() -> None:
    """Configure stdout + file logging with UTC timestamps."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "stockanalysis_scraper.log"
    handler_out = logging.StreamHandler(sys.stdout)
    handler_file = logging.FileHandler(log_path, mode="a")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fmt.converter = time.gmtime
    handler_out.setFormatter(fmt)
    handler_file.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = []
    root.addHandler(handler_out)
    root.addHandler(handler_file)


def load_dotenv(path: Path) -> None:
    """Lightweight .env loader (no external python-dotenv dependency)."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, val)
    except Exception as exc:
        logging.warning("Could not read %s: %s", path, exc)


def load_stage1_tickers(path: Path) -> list[str]:
    """Load uppercase tickers from the Stage 1 survivors file."""
    if not path.exists():
        logging.error("Stage 1 survivors not found: %s", path)
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logging.error("Failed to parse %s: %s", path, exc)
        return []

    tickers: set[str] = set()
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                t = item.get("ticker") or item.get("symbol")
                if isinstance(t, str) and t.strip():
                    tickers.add(t.strip().upper())
            elif isinstance(item, str) and item.strip():
                tickers.add(item.strip().upper())
    elif isinstance(data, dict):
        for k in data.keys():
            if isinstance(k, str) and k.strip():
                tickers.add(k.strip().upper())
    else:
        logging.error("Unexpected Stage 1 survivors format in %s", path)
    return sorted(tickers)


def parse_human_value(value: str | None) -> float | None:
    """Parse values like 9.98B, 448.38M, 12.97%, 1.77, n/a."""
    if value is None:
        return None
    v = value.strip().replace(",", "").replace("$", "")
    if not v or v.lower() in ("n/a", "na", "-", "—"):
        return None
    match = re.match(r"^(-?\d+\.?\d*)([KMBT%]?)\s*$", v)
    if not match:
        return None
    num = float(match.group(1))
    suffix = match.group(2)
    if suffix == "%":
        return num / 100.0
    multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    return num * multiplier.get(suffix, 1)


def parse_statistics_page(html: str) -> dict[str, float | int]:
    """Parse key fields from the StockAnalysis statistics page HTML."""
    soup = BeautifulSoup(html, "lxml")
    label_map = {
        "short_percent_float": "Short % of Float",
        "shares_outstanding": "Shares Outstanding",
        "float_shares": "Float",
        "market_cap": "Market Cap",
        "beta": "Beta (5Y)",
        "average_volume_30d": "Average Volume (20 Days)",
    }

    raw: dict[str, str] = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            label = tds[0].get_text(strip=True)
            value = tds[1].get_text(strip=True)
            if label and label not in raw:
                raw[label] = value

    parsed: dict[str, float | int] = {}
    for field, label in label_map.items():
        if label not in raw:
            continue
        number = parse_human_value(raw[label])
        if number is None:
            continue
        if field in ("shares_outstanding", "float_shares", "average_volume_30d"):
            parsed[field] = int(round(number))
        else:
            parsed[field] = number
    return parsed


def fetch_stockanalysis_statistics(
    ticker: str, session: requests.Session
) -> tuple[dict | None, bool]:
    """
    Fetch and parse the StockAnalysis statistics page.
    Returns (data, scrape_failed).
    """
    url = f"https://stockanalysis.com/stocks/{ticker.lower()}/statistics/"
    try:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        logging.warning("Timeout fetching %s", url)
        return None, True
    except requests.exceptions.RequestException as exc:
        logging.warning("Request error for %s: %s", ticker, exc)
        return None, True

    data = parse_statistics_page(resp.text)
    if not data:
        logging.warning("Could not parse statistics table for %s", ticker)
        return None, True
    return data, False


def fmp_fallback(ticker: str, api_key: str, session: requests.Session) -> dict:
    """
    Attempt to backfill missing fields from FMP stable endpoints.
    Only fills what is available; short interest is included only when present.
    """
    if not api_key:
        return {}

    gathered: dict[str, float | int] = {}
    endpoints = [
        (
            "https://financialmodelingprep.com/stable/profile",
            {
                "beta": "beta",
                "averageVolume": "average_volume_30d",
                "marketCap": "market_cap",
            },
        ),
        (
            "https://financialmodelingprep.com/stable/shares-float",
            {
                "outstandingShares": "shares_outstanding",
                "floatShares": "float_shares",
            },
        ),
        (
            "https://financialmodelingprep.com/stable/short-interest",
            {"shortPercentOfFloat": "short_percent_float"},
        ),
    ]

    for base_url, mapping in endpoints:
        try:
            resp = session.get(
                f"{base_url}?symbol={ticker}&apikey={api_key}",
                timeout=TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, list) or not payload:
                continue
            item = payload[0]
            for src_key, dst_key in mapping.items():
                val = item.get(src_key)
                if val is None:
                    continue
                try:
                    if dst_key in (
                        "shares_outstanding",
                        "float_shares",
                        "average_volume_30d",
                        "market_cap",
                    ):
                        gathered[dst_key] = int(round(float(val)))
                    elif dst_key == "short_percent_float":
                        gathered[dst_key] = float(val)
                    else:
                        gathered[dst_key] = float(val)
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            logging.debug("FMP fallback %s for %s: %s", base_url, ticker, exc)

    return gathered


def atomic_write_json(path: Path, data: dict | list) -> None:
    """Write JSON atomically so readers never see partial files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    tmp.replace(path)


def parse_most_shorted_page(
    html: str, limit: int = 50, min_short: float = 0.15
) -> list[dict]:
    """Parse the StockAnalysis most-shorted table and return top N entries."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        logging.warning("No table found on most-shorted page")
        return []

    rows = table.find_all("tr")
    if not rows:
        return []

    headers = [th.get_text(strip=True) for th in rows[0].find_all("th")]
    try:
        sym_idx = headers.index("Symbol")
        name_idx = headers.index("Company Name")
        short_idx = headers.index("Short % Float")
        mc_idx = headers.index("Market Cap")
    except ValueError:
        sym_idx, name_idx, short_idx, mc_idx = 1, 2, 3, 6

    results: list[dict] = []
    for tr in rows[1:]:
        tds = tr.find_all("td")
        need = max(sym_idx, name_idx, short_idx) + 1
        if len(tds) < need:
            continue
        short_val = parse_human_value(tds[short_idx].get_text(strip=True))
        if short_val is None:
            continue
        if short_val <= min_short:
            continue
        symbol = tds[sym_idx].get_text(strip=True).upper()
        name = tds[name_idx].get_text(strip=True)
        market_cap = None
        if mc_idx < len(tds):
            market_cap = parse_human_value(tds[mc_idx].get_text(strip=True))
        results.append(
            {
                "symbol": symbol,
                "company_name": name,
                "short_percent_float": round(short_val, 6),
                "market_cap": market_cap,
            }
        )
        if len(results) >= limit:
            break

    return results


def fetch_most_shorted_universe(session: requests.Session) -> dict | None:
    """Scrape the most-shorted list and return a structured payload."""
    url = "https://stockanalysis.com/list/most-shorted-stocks/"
    try:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
    except Exception as exc:
        logging.error("Failed to fetch most-shorted list: %s", exc)
        return None

    stocks = parse_most_shorted_page(resp.text)
    return {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": "stockanalysis",
        "count": len(stocks),
        "stocks": stocks,
    }


def build_record(
    ticker: str,
    data: dict,
    scrape_failed: bool,
    source: str,
    fmp_used: bool,
) -> dict:
    record = {
        "ticker": ticker,
        "scrape_failed": scrape_failed,
        "source": source,
        "fmp_fallback_used": fmp_used,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    for field in (
        "short_percent_float",
        "shares_outstanding",
        "float_shares",
        "market_cap",
        "beta",
        "average_volume_30d",
    ):
        record[field] = data.get(field)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="StockAnalysis scraper for System 2")
    parser.add_argument(
        "--tickers",
        type=str,
        default=None,
        help="Comma-separated tickers to scrape (skips stage1_survivors.json)",
    )
    parser.add_argument(
        "--max-tickers",
        type=int,
        default=None,
        help="Process only the first N tickers",
    )
    parser.add_argument(
        "--skip-high-short",
        action="store_true",
        help="Skip the most-shorted list scrape",
    )
    args = parser.parse_args()

    setup_logging()
    load_dotenv(ENV_PATH)
    logging.info("StockAnalysis scraper starting")

    api_key = os.environ.get("FMP_API_KEY")
    if api_key:
        logging.info("FMP_API_KEY loaded (fallback enabled)")
    else:
        logging.info("FMP_API_KEY not found; fallback disabled")

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = load_stage1_tickers(STAGE1_PATH)

    if not tickers:
        logging.error("No tickers to process. Exiting.")
        return 1

    if args.max_tickers:
        tickers = tickers[: args.max_tickers]

    logging.info("Tickers to process: %d", len(tickers))

    # Resume partial results if they exist
    if OUTPUT_PATH.exists():
        try:
            with OUTPUT_PATH.open("r", encoding="utf-8") as fh:
                results: dict = json.load(fh)
            logging.info("Resumed %d existing records", len(results))
        except Exception as exc:
            logging.warning("Could not resume %s: %s", OUTPUT_PATH, exc)
            results = {}
    else:
        results = {}

    session = requests.Session()
    processed = 0
    failed = 0

    for idx, ticker in enumerate(tickers, start=1):
        if idx > 1:
            time.sleep(DELAY_SECONDS)

        logging.info("[%d/%d] Processing %s", idx, len(tickers), ticker)
        data, scrape_failed = fetch_stockanalysis_statistics(ticker, session)

        fmp_used = False
        source = "stockanalysis"
        if scrape_failed or data is None:
            failed += 1
            if api_key:
                logging.info("Attempting FMP fallback for %s", ticker)
                fallback = fmp_fallback(ticker, api_key, session)
                if data is None:
                    data = {}
                for k, v in fallback.items():
                    if data.get(k) is None:
                        data[k] = v
                fmp_used = bool(fallback)
                source = "fmp_fallback" if fmp_used else "stockanalysis"
                if fmp_used:
                    logging.info(
                        "FMP fallback succeeded for %s (fields: %s)",
                        ticker,
                        list(fallback.keys()),
                    )
            else:
                data = {}

        record = build_record(ticker, data, scrape_failed, source, fmp_used)
        results[ticker] = record

        try:
            atomic_write_json(OUTPUT_PATH, {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "scores": results,
            })
        except Exception as exc:
            logging.error("Failed to write %s: %s", OUTPUT_PATH, exc)

        processed += 1

    logging.info("Processed %d tickers (%d failed)", processed, failed)

    if not args.skip_high_short:
        time.sleep(DELAY_SECONDS)
        high_short = fetch_most_shorted_universe(session)
        if high_short is not None:
            try:
                atomic_write_json(HIGH_SHORT_PATH, high_short)
                logging.info(
                    "High-short universe saved: %d stocks", high_short["count"]
                )
            except Exception as exc:
                logging.error("Failed to write %s: %s", HIGH_SHORT_PATH, exc)

    logging.info("StockAnalysis scraper complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
