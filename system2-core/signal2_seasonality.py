#!/usr/bin/env python3
"""Signal 2 — Seasonality enrichment for System 2 finalists.

Ride-along only. No scoring impact until proven in the loop.
Fetches last 5 years of daily EOD from FMP, groups by month,
and calculates historical performance for the current month.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import fmp_cache


ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path.home() / "Downloads"
INPUT_PATH = ROOT / "stage2_surgical_strike_top40.json"
OUTPUT_PATH = ROOT / "stage2_seasonality_enriched.json"
META_PATH = ROOT / "signal2_seasonality_metadata.json"
FMP_BASE = "https://financialmodelingprep.com"


def load_fmp_key() -> str:
    env_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if env_key:
        return env_key.strip()
    for path in [DOWNLOADS / "FMP-Scanner-v13.5-alpaca.json", DOWNLOADS / "FMP_Scanner_FIXED.json"]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"FMP_API_KEY:\s*'([^']+)'", text)
        if match:
            return match.group(1)
    raise RuntimeError("FMP API key not found. Set FMP_API_KEY.")


class FmpClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.calls = 0
        self.errors: list[str] = []

    def get(self, endpoint: str, timeout: int = 30):
        use_daily_cache = endpoint.startswith("stable/historical-price-eod/full")
        if use_daily_cache:
            cached = fmp_cache.get_daily(endpoint)
            if cached is not None:
                return cached
        sep = "&" if "?" in endpoint else "?"
        url = f"{FMP_BASE}/{endpoint}{sep}apikey={urllib.parse.quote(self.api_key)}"
        self.calls += 1
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "system2-signal2/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore"))
                    if use_daily_cache:
                        fmp_cache.set_daily(endpoint, data)
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: HTTP {exc.code}")
                return None
            except Exception as exc:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: {exc}")
                return None
        return None


def num(value, default=None) -> float | None:
    try:
        if value in (None, ""):
            return default
        v = float(value)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def monthly_returns(bars: list[dict]) -> list[dict]:
    """Group daily bars by calendar month and compute monthly return."""
    months: dict[str, dict] = {}
    for bar in bars:
        d = str(bar.get("date") or "")[:10]
        if not d:
            continue
        month_key = d[:7]  # YYYY-MM
        if month_key not in months:
            months[month_key] = {"open": num(bar.get("open")), "close": num(bar.get("close")), "first_date": d}
        months[month_key]["close"] = num(bar.get("close"))
    results = []
    for month_key in sorted(months):
        m = months[month_key]
        o = m.get("open")
        c = m.get("close")
        if o and c and o > 0:
            results.append({"month": month_key, "return_pct": round(((c - o) / o) * 100, 4)})
    return results


def seasonality_for(ticker: str, client: FmpClient) -> dict[str, Any]:
    today = date.today()
    start = today.replace(year=today.year - 5)
    data = client.get(
        f"stable/historical-price-eod/full?symbol={urllib.parse.quote(ticker)}"
        f"&from={start.isoformat()}&to={today.isoformat()}"
    )
    if not isinstance(data, list):
        return {
            "seasonal_avg_return": None,
            "seasonal_win_rate": None,
            "seasonal_years_count": 0,
            "seasonal_signal": "INSUFFICIENT_DATA",
            "seasonal_error": "no data",
        }
    bars = [row for row in data if isinstance(row, dict) and row.get("date")]
    months = monthly_returns(bars)
    current_month = today.strftime("%m")  # e.g. "06"
    same_month = [m for m in months if m["month"][5:7] == current_month]
    if len(same_month) < 3:
        return {
            "seasonal_avg_return": None,
            "seasonal_win_rate": None,
            "seasonal_years_count": len(same_month),
            "seasonal_signal": "INSUFFICIENT_DATA",
            "seasonal_error": f"only {len(same_month)} years of data for month {current_month}",
        }
    returns = [m["return_pct"] for m in same_month]
    avg = round(sum(returns) / len(returns), 4)
    wins = sum(1 for r in returns if r > 0)
    win_rate = round((wins / len(returns)) * 100, 2)
    if avg > 1.0 and win_rate > 60.0:
        signal = "TAILWIND"
    elif avg < -1.0 and win_rate < 40.0:
        signal = "HEADWIND"
    else:
        signal = "NEUTRAL"
    return {
        "seasonal_avg_return": avg,
        "seasonal_win_rate": win_rate,
        "seasonal_years_count": len(same_month),
        "seasonal_signal": signal,
        "seasonal_error": None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(INPUT_PATH))
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--metadata", default=str(META_PATH))
    args = parser.parse_args()

    api_key = load_fmp_key()
    client = FmpClient(api_key)

    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    enriched = []
    distribution: dict[str, int] = {}

    for idx, row in enumerate(rows, 1):
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
        if not symbol:
            enriched.append(row)
            continue
        if idx % 20 == 0:
            time.sleep(1)
        result = seasonality_for(symbol, client)
        signal = result["seasonal_signal"]
        distribution[signal] = distribution.get(signal, 0) + 1
        enriched.append({**row, **result})

    metadata = {
        "stage": "SIGNAL2_SEASONALITY",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "inputCount": len(rows),
        "outputCount": len(enriched),
        "fmpCalls": client.calls,
        "fmpErrors": client.errors,
        "currentMonth": date.today().strftime("%m"),
        "signalDistribution": distribution,
        "mode": "ride_along_logging_only",
        "paperOnly": True,
    }

    Path(args.output).write_text(json.dumps(enriched, indent=2), encoding="utf-8")
    Path(args.metadata).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
