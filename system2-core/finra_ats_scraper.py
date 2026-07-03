#!/usr/bin/env python3
"""FINRA RegSHO short-volume scraper (daily). Used as dark-pool proxy.

FINRA publishes daily short-sale volume files. Short-sale volume is
largely executed off-exchange, so short_volume_pct serves as a practical
proxy for elevated dark-pool / off-exchange activity.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "data" / "finra_dark_pool.json"
UNIVERSE_PATH = ROOT / "universe.json"
LOG_DIR = ROOT / "logs"

DAYS_TO_AGGREGATE = 5
MAX_LOOKBACK_DAYS = 12
URL_TEMPLATE = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt"


def load_universe() -> set[str]:
    """Load the current System 2 universe as an upper-case set."""
    if not UNIVERSE_PATH.exists():
        return set()
    try:
        data = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(t).upper().strip() for t in data if t}
        if isinstance(data, dict) and "tickers" in data:
            return {str(t).upper().strip() for t in data["tickers"] if t}
    except Exception as exc:
        print(f"WARNING: could not load universe: {exc}")
    return set()


def fetch_daily_file(date: datetime) -> list[str] | None:
    """Download FINRA daily RegSHO file for the given date."""
    date_str = date.strftime("%Y%m%d")
    url = URL_TEMPLATE.format(YYYYMMDD=date_str)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=60)
        if resp.status_code == 404:
            print(f"FINRA daily file not yet available for {date_str} (404)")
            return None
        if resp.status_code == 403:
            print(f"FINRA daily file restricted for {date_str} (403)")
            return None
        resp.raise_for_status()
        return resp.text.splitlines()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 404:
            print(f"FINRA daily file not yet available for {date_str} (404)")
            return None
        if status == 403:
            print(f"FINRA daily file restricted for {date_str} (403)")
            return None
        raise
    except requests.exceptions.Timeout:
        print(f"FINRA daily file timeout for {date_str}")
        return None


def parse_regsho_file(lines: list[str], universe: set[str]) -> dict[str, dict]:
    """Parse pipe-delimited FINRA RegSHO file and keep universe tickers."""
    rows: dict[str, dict] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("Date|") or line.startswith("Symbol|"):
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        # Daily format: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
        if parts[0].strip().isdigit() and len(parts[0].strip()) == 8:
            symbol = parts[1].strip().upper()
            short_volume_idx = 2
            total_volume_idx = 4
        else:
            symbol = parts[0].strip().upper()
            short_volume_idx = 1
            total_volume_idx = 3

        if universe and symbol not in universe:
            continue
        try:
            short_volume = int(float(parts[short_volume_idx].strip() or 0))
            total_volume = int(float(parts[total_volume_idx].strip() or 0))
        except ValueError:
            continue
        if total_volume <= 0:
            continue
        entry = rows.setdefault(symbol, {"short_volume": 0, "total_volume": 0})
        entry["short_volume"] += short_volume
        entry["total_volume"] += total_volume
    return rows


def classify_signal(pct: float) -> str:
    if pct > 60:
        return "STRONG"
    if pct > 45:
        return "MODERATE"
    if pct > 30:
        return "WEAK"
    return "NONE"


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    universe = load_universe()
    print(f"Universe loaded: {len(universe)} tickers")

    today = datetime.now(timezone.utc)
    aggregated: dict[str, dict] = {}
    collected_dates: list[str] = []

    for offset in range(MAX_LOOKBACK_DAYS):
        if len(collected_dates) >= DAYS_TO_AGGREGATE:
            break
        date = today - timedelta(days=offset)
        lines = fetch_daily_file(date)
        if lines is None:
            continue
        daily = parse_regsho_file(lines, universe)
        if not daily:
            continue
        collected_dates.append(date.strftime("%Y-%m-%d"))
        for symbol, vals in daily.items():
            entry = aggregated.setdefault(symbol, {"short_volume": 0, "total_volume": 0})
            entry["short_volume"] += vals["short_volume"]
            entry["total_volume"] += vals["total_volume"]

    if not aggregated:
        print("ERROR: could not fetch any recent FINRA daily file")
        sys.exit(1)

    stocks: dict[str, dict] = {}
    for symbol, vals in aggregated.items():
        if vals["total_volume"] <= 0:
            continue
        pct = (vals["short_volume"] / vals["total_volume"]) * 100.0
        stocks[symbol] = {
            "dark_pool_volume": vals["short_volume"],
            "total_volume": vals["total_volume"],
            "dark_pool_pct": round(pct, 2),
            "dark_pool_signal": classify_signal(pct),
        }

    print(f"Aggregated {len(collected_dates)} trading day(s): {collected_dates}")
    print(f"Parsed dark-pool data for {len(stocks)} universe tickers")

    strong = sum(1 for s in stocks.values() if s["dark_pool_signal"] == "STRONG")
    moderate = sum(1 for s in stocks.values() if s["dark_pool_signal"] == "MODERATE")
    weak = sum(1 for s in stocks.values() if s["dark_pool_signal"] == "WEAK")
    print(f"Signals: STRONG={strong}, MODERATE={moderate}, WEAK={weak}")

    output = {
        "date": collected_dates[0] if collected_dates else today.strftime("%Y-%m-%d"),
        "aggregation_dates": collected_dates,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "finra_regsho_daily",
        "total_symbols": len(stocks),
        "elevated_count": strong + moderate,
        "strong_count": strong,
        "moderate_count": moderate,
        "weak_count": weak,
        "stocks": stocks,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
