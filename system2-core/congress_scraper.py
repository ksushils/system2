#!/usr/bin/env python3
"""Congressional trading discovery scraper for System 2.

Identifies tickers with recent congressional purchase activity that bypass
the B2/B3 technical gate and enter as Set 3 catalyst candidates.

Sources (free public CSVs):
  - https://housestockwatcher.com/api/v1/raw/export.csv
  - https://senatestockwatcher.com/api/v1/raw/export.csv
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "data" / "congress_discovery.json"
CONGRESS_TRADES_PATH = ROOT / "data" / "congress_trades.json"
META_PATH = ROOT / "data" / "congress_discovery_metadata.json"
LOG_DIR = ROOT / "logs"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

SOURCES = [
    ("house", "https://housestockwatcher.com/api/v1/raw/export.csv"),
    ("senate", "https://senatestockwatcher.com/api/v1/raw/export.csv"),
]
CAPITOLTRADES_URL = "https://www.capitoltrades.com/trades"
KADOA_TRADES_URL = (
    "https://raw.githubusercontent.com/kadoa-org/"
    "congress-trading-monitor/main/public/data/trades.json"
)

CLUSTER_MIN_POLITICIANS = 2
STRONG_MIN_POLITICIANS = 3
LOOKBACK_DAYS = 60


def clean_symbol(symbol: Any) -> str:
    return re.sub(r"[^A-Z0-9.]", "", str(symbol or "").upper()).strip(".")


def parse_amount(value: Any) -> float:
    """Convert amount string like '$1,001 - $15,000' to midpoint."""
    s = str(value or "").replace(",", "").replace("$", "")
    nums = re.findall(r"[\d.]+", s)
    if not nums:
        return 0.0
    vals = [float(n) for n in nums]
    return sum(vals) / len(vals)


def parse_date(value: Any) -> str:
    s = str(value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def is_purchase(row: dict[str, str]) -> bool:
    t = " ".join(str(row.get(k, "")).lower() for k in row)
    return "purchase" in t or "buy" in t


def fetch_csv(url: str) -> list[dict[str, str]]:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    text = resp.content.decode("utf-8", "ignore")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def fetch_capitoltrades_rows() -> list[dict[str, Any]]:
    """Fallback to CapitolTrades rendered HTML when legacy CSV hosts are down."""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    resp = requests.get(CAPITOLTRADES_URL, headers=headers, timeout=60)
    resp.raise_for_status()
    text = html.unescape(resp.text)
    text = re.sub(r"<script[\s\S]*?</script>", "\n", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "\n", text, flags=re.I)
    lines = [ln.strip() for ln in re.sub(r"<[^>]+>", "\n", text).splitlines() if ln.strip()]

    rows: list[dict[str, Any]] = []
    ticker_re = re.compile(r"^[A-Z][A-Z0-9./-]{0,9}:US$")
    for idx, line in enumerate(lines):
        if not re.match(r"^[A-Z][A-Za-z .'-]{2,80}$", line):
            continue
        chamber_line = lines[idx + 1] if idx + 1 < len(lines) else ""
        chamber = "house" if "House" in chamber_line else ("senate" if "Senate" in chamber_line else "")
        if not chamber:
            continue
        window = lines[idx + 2:idx + 28]
        ticker_pos = next((pos for pos, val in enumerate(window) if ticker_re.match(val)), None)
        if ticker_pos is None:
            continue
        symbol = clean_symbol(window[ticker_pos].split(":", 1)[0])
        trade_type_pos = next(
            (pos for pos, val in enumerate(window[ticker_pos + 1:], start=ticker_pos + 1)
             if val.lower() in {"buy", "purchase", "sell", "sale", "exchange"}),
            None,
        )
        if trade_type_pos is None or window[trade_type_pos].lower() not in {"buy", "purchase"}:
            continue
        amount = parse_amount(window[trade_type_pos + 1] if trade_type_pos + 1 < len(window) else "")
        traded_date = ""
        for pos in range(ticker_pos + 1, min(trade_type_pos, len(window) - 2)):
            candidate = " ".join(window[pos:pos + 3])
            if re.match(r"^\d{1,2} [A-Z][a-z]{2} \d{4}$", candidate):
                traded_date = parse_date(datetime.strptime(candidate, "%d %b %Y").strftime("%Y-%m-%d"))
                break
        rows.append({
            "symbol": symbol,
            "politician": line,
            "date": traded_date,
            "amount": amount,
            "chamber": chamber,
        })
    return rows[:250]


def fetch_kadoa_rows() -> list[dict[str, Any]]:
    """Fetch open static STOCK Act trade data from kadoa-org."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    resp = requests.get(KADOA_TRADES_URL, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        if str(row.get("branch") or "").lower() != "congress":
            continue
        if "purchase" not in str(row.get("transaction_type") or "").lower():
            continue
        symbol = clean_symbol(row.get("ticker"))
        if not symbol:
            continue
        amount_low = float(row.get("amount_range_low") or 0)
        amount_high = float(row.get("amount_range_high") or amount_low or 0)
        rows.append({
            "symbol": symbol,
            "politician": row.get("filer_name") or "Unknown",
            "date": parse_date(row.get("transaction_date") or ""),
            "amount": (amount_low + amount_high) / 2 if amount_high else amount_low,
            "chamber": str(row.get("chamber") or "").lower(),
        })
    return rows[:5000]


def load_all_transactions() -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    for chamber, url in SOURCES:
        try:
            rows = fetch_csv(url)
        except Exception as exc:
            print(f"WARN: could not fetch {chamber} CSV: {exc}")
            continue
        for row in rows:
            symbol = clean_symbol(row.get("ticker") or row.get("Ticker"))
            if not symbol:
                continue
            if not is_purchase(row):
                continue
            name = (
                row.get("representative") or row.get("senator")
                or row.get("Representative") or row.get("Senator")
                or row.get("name") or row.get("Name") or "Unknown"
            )
            date = parse_date(row.get("transaction_date") or row.get("transactionDate") or row.get("Date") or "")
            amount = parse_amount(row.get("amount") or row.get("Amount"))
            all_rows.append({
                "symbol": symbol,
                "politician": name,
                "date": date,
                "amount": amount,
                "chamber": chamber,
            })
    if not all_rows:
        try:
            rows = fetch_kadoa_rows()
            print(f"Loaded {len(rows)} Kadoa fallback rows")
            all_rows.extend(rows)
        except Exception as exc:
            print(f"WARN: could not fetch Kadoa fallback: {exc}")
    if not all_rows:
        try:
            rows = fetch_capitoltrades_rows()
            print(f"Loaded {len(rows)} CapitolTrades fallback rows")
            all_rows.extend(rows)
        except Exception as exc:
            print(f"WARN: could not fetch CapitolTrades fallback: {exc}")
    return all_rows


def aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_ticker: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"buy_count": 0, "total_amount": 0.0, "politicians": set(), "chambers": set()}
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    for row in rows:
        if row["date"] and row["date"] < cutoff:
            continue
        sym = row["symbol"]
        by_ticker[sym]["buy_count"] += 1
        by_ticker[sym]["total_amount"] += row["amount"]
        by_ticker[sym]["politicians"].add(row["politician"])
        by_ticker[sym]["chambers"].add(row["chamber"])
    return dict(by_ticker)


def build_congress_trades_output(tickers: dict[str, dict[str, Any]], today: str) -> dict[str, Any]:
    normalized: dict[str, dict[str, Any]] = {}
    for ticker, data in tickers.items():
        politicians = sorted(data.get("politicians") or [])
        chambers = sorted(data.get("chambers") or [])
        count = int(data.get("buy_count") or 0)
        is_cluster = len(politicians) >= CLUSTER_MIN_POLITICIANS or count >= CLUSTER_MIN_POLITICIANS
        is_bipartisan = len(chambers) > 1
        if is_cluster and is_bipartisan:
            signal = "STRONG"
        elif is_cluster:
            signal = "MODERATE"
        elif count > 0:
            signal = "WEAK"
        else:
            signal = "NONE"
        normalized[ticker] = {
            "congress_buy_count": count,
            "congress_total_value": round(float(data.get("total_amount") or 0), 2),
            "politicians": [{"name": p, "chamber": "", "value": 0, "date": ""} for p in politicians[:10]],
            "politician_names": politicians[:10],
            "chambers": chambers,
            "congress_signal": signal,
            "is_cluster": is_cluster,
            "is_bipartisan": is_bipartisan,
        }
    return {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "congress_public_disclosures",
        "total_tickers": len(normalized),
        "cluster_buys": sum(1 for v in normalized.values() if v.get("is_cluster")),
        "bipartisan_buys": sum(1 for v in normalized.values() if v.get("is_bipartisan")),
        "tickers": normalized,
    }


def run() -> dict[str, Any]:
    """Run congressional discovery and return output payload."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()

    try:
        rows = load_all_transactions()
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
        politician_count = len(data["politicians"])
        is_cluster = politician_count >= CLUSTER_MIN_POLITICIANS
        congress_signal = "STRONG" if politician_count >= STRONG_MIN_POLITICIANS else ("MODERATE" if is_cluster else "WEAK")
        if not is_cluster and congress_signal != "STRONG":
            continue
        bypass.append({
            "ticker": ticker,
            "symbol": ticker,
            "source": "congress_discovery",
            "sub_type": "congressional_buy",
            "catalyst_summary": (
                f"{politician_count} politician(s) bought"
            ),
            "bypass_technical": True,
            "bypass_reason": "congressional_cluster",
            "congress_buy_count": data["buy_count"],
            "congress_politician_count": politician_count,
            "congress_total_amount": round(data["total_amount"], 2),
            "congress_signal": congress_signal,
            "politicians": sorted(data["politicians"])[:5],
            "chambers": sorted(data["chambers"]),
        })

    output = {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(bypass),
        "candidates": bypass,
    }
    trades_output = build_congress_trades_output(tickers, today)
    CONGRESS_TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONGRESS_TRADES_PATH.write_text(json.dumps(trades_output, indent=2), encoding="utf-8")
    return output


def main() -> None:
    output = run()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")

    metadata = {
        "stage": "CONGRESS_DISCOVERY",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "candidateCount": output["count"],
        "paper_only": True,
    }
    if "error" in output:
        metadata["error"] = output["error"]
    META_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(json.dumps({
        "congress_bypass_candidates": output["count"],
        "top3": output["candidates"][:3],
    }, indent=2))


if __name__ == "__main__":
    main()
