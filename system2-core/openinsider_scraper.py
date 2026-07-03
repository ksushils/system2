#!/usr/bin/env python3
"""OpenInsider cluster-buy discovery scraper for System 2.

Identifies tickers with recent cluster insider buying that bypass the
B2/B3 technical gate and enter as Set 3 catalyst candidates.

Source: http://openinsider.com (free public pages)
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "data" / "insider_discovery.json"
INSIDER_TRADES_PATH = ROOT / "data" / "insider_trades.json"
META_PATH = ROOT / "data" / "insider_discovery_metadata.json"
LOG_DIR = ROOT / "logs"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# OpenInsider screener: purchase trades, grouped by ticker, last 15 days.
SCREEN_URL = (
    "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=15&fdr=&td=0&tdr="
    "&fdlyl=&fdlyh=&daysago=&xp=1&xsCode=1&sc=&isofficer=1&iscob=1&isceo=1"
    "&ispres=1&iscoo=1&iscfo=1&isgc=1&isvp=1&isdirector=1&istenpercent=1"
    "&isother=1&grp=2&fn=&mt=2&lobt=0&lotbt=0&sf=0&sd=1&st=1"
)

CLUSTER_MIN_BUYERS = 2
CLUSTER_MIN_VALUE = 100_000


def parse_money(value: Any) -> float:
    s = str(value or "").replace(",", "").replace("$", "").replace("+", "").strip()
    multipliers = {"K": 1e3, "M": 1e6, "B": 1e9}
    if s and s[-1].upper() in multipliers:
        try:
            return float(s[:-1]) * multipliers[s[-1].upper()]
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_int(value: Any) -> int:
    try:
        return int(float(str(value or "").replace(",", "").replace("+", "").strip()))
    except (TypeError, ValueError):
        return 0


def clean_symbol(symbol: Any) -> str:
    return re.sub(r"[^A-Z0-9.]", "", str(symbol or "").upper()).strip(".")


def fetch_screener_rows() -> list[dict[str, Any]]:
    """Scrape OpenInsider grouped insider trading results."""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    resp = requests.get(SCREEN_URL, headers=headers, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table", class_="tinytable")
    if table is None:
        table = soup.find("table")
    if table is None:
        return []

    rows: list[dict[str, Any]] = []
    headers_seen = [th.get_text(strip=True) for th in table.find_all("th")]
    header_index = {h: i for i, h in enumerate(headers_seen)}

    def col(texts, name, default=""):
        idx = header_index.get(name)
        return texts[idx] if idx is not None and idx < len(texts) else default

    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        texts = [td.get_text(" ", strip=True) for td in tds]
        symbol = clean_symbol(col(texts, "Ticker"))
        if not symbol or symbol in {"THE", "AND"} or len(symbol) < 1:
            continue
        rows.append({
            "symbol": symbol,
            "insider_count": parse_int(col(texts, "Ins")),
            "value": parse_money(col(texts, "Value")),
            "qty": parse_int(col(texts, "Qty")),
            "trade_date": col(texts, "Trade Date"),
        })
    return rows


def aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_ticker: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"insider_buy_value": 0.0, "insider_buy_count": 0, "unique_insiders": 0, "qty": 0}
    )
    for row in rows:
        sym = row["symbol"]
        by_ticker[sym]["insider_buy_value"] += row["value"]
        by_ticker[sym]["insider_buy_count"] += 1
        by_ticker[sym]["unique_insiders"] = max(by_ticker[sym]["unique_insiders"], row["insider_count"])
        by_ticker[sym]["qty"] += row["qty"]
    return dict(by_ticker)


def build_insider_trades_output(tickers: dict[str, dict[str, Any]], today: str) -> dict[str, Any]:
    normalized: dict[str, dict[str, Any]] = {}
    for ticker, data in tickers.items():
        count = int(data.get("insider_buy_count") or 0)
        unique = int(data.get("unique_insiders") or 0)
        value = float(data.get("insider_buy_value") or 0)
        is_cluster = unique >= CLUSTER_MIN_BUYERS or count >= CLUSTER_MIN_BUYERS
        if is_cluster or value >= 500_000:
            signal = "STRONG"
        elif value >= 200_000:
            signal = "MODERATE"
        elif value > 0:
            signal = "WEAK"
        else:
            signal = "NONE"
        normalized[ticker] = {
            "insider_buy_count": count,
            "insider_buy_value": round(value, 2),
            "unique_insiders": unique,
            "is_cluster": is_cluster,
            "insider_buy_signal": signal,
            "buyers": [],
            "most_recent_date": today,
        }
    return {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "openinsider_sec_form4",
        "total_tickers": len(normalized),
        "cluster_buys": sum(1 for v in normalized.values() if v.get("is_cluster")),
        "strong_signals": sum(1 for v in normalized.values() if v.get("insider_buy_signal") == "STRONG"),
        "tickers": normalized,
    }


def run() -> dict[str, Any]:
    """Run OpenInsider discovery and return output payload."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()

    try:
        rows = fetch_screener_rows()
        tickers = aggregate(rows)
    except Exception as exc:
        return {
            "date": today,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": 0,
            "candidates": [],
            "error": str(exc),
        }

    bypass: list[dict[str, Any]] = []
    for ticker, data in tickers.items():
        unique_insiders = data["unique_insiders"]
        is_cluster = unique_insiders >= CLUSTER_MIN_BUYERS or data["insider_buy_value"] >= CLUSTER_MIN_VALUE
        if not is_cluster:
            continue
        bypass.append({
            "ticker": ticker,
            "symbol": ticker,
            "source": "insider_discovery",
            "sub_type": "cluster_insider_buy",
            "catalyst_summary": (
                f"{unique_insiders} insider(s) bought $"
                f"{data['insider_buy_value']:,.0f}"
            ),
            "bypass_technical": True,
            "bypass_reason": "insider_cluster_buy",
            "insider_buy_value": round(data["insider_buy_value"], 2),
            "insider_buy_count": data["insider_buy_count"],
            "insider_buy_qty": data["qty"],
            "unique_insiders": unique_insiders,
        })

    output = {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(bypass),
        "candidates": bypass,
    }
    trades_output = build_insider_trades_output(tickers, today)
    INSIDER_TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSIDER_TRADES_PATH.write_text(json.dumps(trades_output, indent=2), encoding="utf-8")
    return output


def main() -> None:
    output = run()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")

    metadata = {
        "stage": "INSIDER_DISCOVERY",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "candidateCount": output["count"],
        "paper_only": True,
    }
    if "error" in output:
        metadata["error"] = output["error"]
    META_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(json.dumps({
        "insider_bypass_candidates": output["count"],
        "top3": output["candidates"][:3],
    }, indent=2))


if __name__ == "__main__":
    main()
