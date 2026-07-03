#!/usr/bin/env python3
"""
B2 Stage 1 cheap filter.

Reads universe.json and writes:
  - stage1_survivors.json
  - stage1_details.json
  - stage1_metadata.json

Rules:
  - reject avg volume < 1M
  - reject price < $5
  - reject dollar volume < $20M/day
  - reject earnings blackout next 5 calendar days
  - reject funds/ETFs/inactive names after a ticker passes the numeric gates

Batch quotes for the broad universe, profile calls only for quote-missing
tickers and numeric survivors, plus one global earnings-calendar call.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import fmp_cache


ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path.home() / "Downloads"
UNIVERSE_PATH = ROOT / "universe.json"
CANDIDATE_POOL_PATH = ROOT / "candidate_pool.json"
SURVIVORS_PATH = ROOT / "stage1_survivors.json"
DETAILS_PATH = ROOT / "stage1_details.json"
META_PATH = ROOT / "stage1_metadata.json"
SHADOW_BLOCKLIST_PATH = ROOT / "shadow_reentry_blocklist.json"

FMP_BASE = "https://financialmodelingprep.com"
PROFILE_TIMEOUT_SECONDS = 12
MAX_CONSECUTIVE_FMP_FAILURES = 5


def load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_config() -> dict:
    path = ROOT / "system2-config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


CONFIG = load_config()
STAGE1_CONFIG = CONFIG.get("stage1", {})
MIN_AVG_VOLUME = int(STAGE1_CONFIG.get("min_avg_volume", 1_000_000))
MIN_PRICE = float(STAGE1_CONFIG.get("min_price", 5))
MIN_DOLLAR_VOLUME = int(STAGE1_CONFIG.get("min_dollar_volume", 20_000_000))
EARNINGS_BLACKOUT_DAYS = int(STAGE1_CONFIG.get("earnings_blackout_days", 5))
BLOCKED_TICKERS = set(STAGE1_CONFIG.get("blocked_tickers", ["STRC"]))


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
        self.consecutive_failures = 0
        self.circuit_open = False

    def get(self, endpoint: str, timeout: int = 30):
        if self.circuit_open:
            return None
        sep = "&" if "?" in endpoint else "?"
        url = f"{FMP_BASE}/{endpoint}{sep}apikey={urllib.parse.quote(self.api_key)}"
        self.calls += 1
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "system2-b2/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore"))
                    self.consecutive_failures = 0
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                self.record_error(f"{endpoint}: HTTP {exc.code}")
                return None
            except Exception as exc:
                if attempt < 2:
                    time.sleep(1 * (attempt + 1))
                    continue
                self.record_error(f"{endpoint}: {exc}")
                return None
        return None

    def record_error(self, message: str) -> None:
        self.errors.append(message)
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FMP_FAILURES:
            self.circuit_open = True
            print(
                f"B2 FMP circuit breaker opened after {self.consecutive_failures} consecutive failures: {message}",
                flush=True,
            )


def as_num(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def clean_symbol(value) -> str:
    return re.sub(r"[^A-Z0-9.]", "", str(value or "").upper()).strip(".")


def load_candidates() -> tuple[list[dict], str]:
    path = CANDIDATE_POOL_PATH if CANDIDATE_POOL_PATH.exists() else UNIVERSE_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))
    candidates: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            symbol = clean_symbol(item.get("symbol") or item.get("ticker"))
            if not symbol:
                continue
            row = {**item, "symbol": symbol, "ticker": symbol}
            row.setdefault("source", "scanner")
            candidates.append(row)
        else:
            symbol = clean_symbol(item)
            if symbol:
                candidates.append({"symbol": symbol, "ticker": symbol, "source": "scanner"})
    deduped: dict[str, dict] = {}
    for row in candidates:
        deduped[row["symbol"]] = {**deduped.get(row["symbol"], {}), **row}
    return list(deduped.values()), path.name


def load_reentry_blocklist() -> dict[str, dict]:
    if not SHADOW_BLOCKLIST_PATH.exists():
        return {}
    rows = json.loads(SHADOW_BLOCKLIST_PATH.read_text(encoding="utf-8"))
    return {
        str(row.get("symbol") or "").upper(): row
        for row in rows
        if isinstance(row, dict) and row.get("symbol")
    }


def chunked(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_batch_quotes(client: FmpClient, symbols: list[str]) -> dict[str, dict]:
    """Fetch live quote fields in broad batches."""
    quote_map: dict[str, dict] = {}
    unique_symbols = sorted({s for s in symbols if s})
    for batch in chunked(unique_symbols, 100):
        endpoint = "stable/batch-quote?symbols=" + ",".join(batch)
        data = client.get(endpoint)
        if not isinstance(data, list):
            continue
        for row in data:
            symbol = clean_symbol(row.get("symbol"))
            if symbol:
                quote_map[symbol] = row
    return quote_map


def fetch_profile(client: FmpClient, symbol: str):
    endpoint = f"stable/profile?symbol={urllib.parse.quote(symbol)}"
    cached = fmp_cache.get_daily(endpoint)
    data = cached if cached is not None else client.get(endpoint, timeout=PROFILE_TIMEOUT_SECONDS)
    if cached is None and data is not None:
        fmp_cache.set_daily(endpoint, data)
    return data


def fetch_bulk_profiles(client: FmpClient) -> dict[str, dict]:
    """Fetch FMP profile-bulk once per day and index locally by symbol."""
    endpoint = "stable/profile-bulk?part=0"
    cached = fmp_cache.get_daily(endpoint)
    data = cached if cached is not None else client.get(endpoint, timeout=90)
    if cached is None and data is not None:
        fmp_cache.set_daily(endpoint, data)
    if isinstance(data, dict):
        rows = data.get("data") or data.get("profiles") or data.get("historical") or []
    else:
        rows = data
    out: dict[str, dict] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = clean_symbol(row.get("symbol"))
            if symbol:
                out[symbol] = row
    return out


def main() -> None:
    started = time.time()
    load_env_file()
    api_key = load_fmp_key()
    client = FmpClient(api_key)
    candidates, input_name = load_candidates()
    reentry_blocklist = load_reentry_blocklist()

    today = date.today()
    to_date = today + timedelta(days=EARNINGS_BLACKOUT_DAYS)
    earnings_data = client.get(f"stable/earnings-calendar?from={today.isoformat()}&to={to_date.isoformat()}")
    earnings_by_symbol = {
        str(row.get("symbol", "")).upper(): row
        for row in earnings_data
        if isinstance(row, dict) and row.get("symbol")
    } if isinstance(earnings_data, list) else {}
    earnings_symbols = set(earnings_by_symbol)

    details = []
    survivors = []
    profile_call_count = 0
    bulk_profile_call_count = 0
    profile_fallback_call_count = 0
    bulk_profile_map: dict[str, dict] | None = None
    total_candidates = len(candidates)
    quote_map = fetch_batch_quotes(client, [c["symbol"] for c in candidates])
    quote_call_count = (len({c["symbol"] for c in candidates}) + 99) // 100

    for idx, candidate in enumerate(candidates, 1):
        if idx == 1 or (idx - 1) % 100 == 0:
            done = idx - 1
            pct = round((done / total_candidates) * 100, 1) if total_candidates else 100
            print(
                f"B2 progress: {done}/{total_candidates} ({pct}%) kept={len(survivors)} "
                f"fmp_calls={client.calls} fmp_errors={len(client.errors)}",
                flush=True,
            )
        symbol = candidate["symbol"]
        if idx % 75 == 0:
            time.sleep(1)
        row = {
            **candidate,
            "symbol": symbol,
            "status": "REJECT",
            "rejectReasons": [],
        }
        prior_rejection = reentry_blocklist.get(symbol)
        if prior_rejection:
            row["rejectReasons"].append("recent_shadow_rejection")
            row["reentryBlocked"] = True
            row["reentryBlockDetail"] = prior_rejection
            row["reentryBlockLog"] = (
                f"Re-entry blocked: {symbol} rejected "
                f"{prior_rejection.get('trading_days_ago')} days ago at stage "
                f"{prior_rejection.get('rejection_stage')}"
            )
            print(row["reentryBlockLog"])
            details.append(row)
            continue
        if symbol in BLOCKED_TICKERS:
            row["rejectReasons"].append("blocked_non_common_equity")
            details.append(row)
            continue
        if client.circuit_open:
            row["rejectReasons"].append("fmp_circuit_open")
            details.append(row)
            continue

        quote = quote_map.get(symbol) or {}
        price = as_num(quote.get("price"))
        avg_volume = as_num(quote.get("avgVolume") or quote.get("averageVolume") or quote.get("volume"))
        volume = as_num(quote.get("volume"))
        item = {}
        if not quote:
            if bulk_profile_map is None:
                bulk_profile_call_count += 1
                bulk_profile_map = fetch_bulk_profiles(client)
            item = (bulk_profile_map or {}).get(symbol) or {}
            if not item:
                profile_call_count += 1
                profile_fallback_call_count += 1
                profile = fetch_profile(client, symbol)
                item = profile[0] if isinstance(profile, list) and profile else {}
            price = as_num(item.get("price"))
            avg_volume = as_num(item.get("averageVolume") or item.get("avgVolume") or item.get("volume"))
            volume = as_num(item.get("volume"))
        dollar_volume = price * avg_volume

        row.update({
            "companyName": quote.get("name") or item.get("companyName") or item.get("name"),
            "sector": item.get("sector"),
            "marketCap": as_num(item.get("marketCap") or item.get("mktCap")),
            "price": price,
            "averageVolume": avg_volume,
            "volume": volume,
            "dollarVolume": round(dollar_volume, 2),
            "earningsBlackoutNext5d": symbol in earnings_symbols,
            "earningsDate": (earnings_by_symbol.get(symbol) or {}).get("date"),
        })

        if price < MIN_PRICE:
            row["rejectReasons"].append("price_below_5")
        if avg_volume < MIN_AVG_VOLUME:
            row["rejectReasons"].append("avg_volume_below_1m")
        if dollar_volume < MIN_DOLLAR_VOLUME:
            row["rejectReasons"].append("dollar_volume_below_20m")
        if symbol in earnings_symbols:
            row["rejectReasons"].append(f"earnings_blackout_next_{EARNINGS_BLACKOUT_DAYS}d")

        needs_profile = not row["rejectReasons"] and not item
        if needs_profile:
            if bulk_profile_map is None:
                bulk_profile_call_count += 1
                bulk_profile_map = fetch_bulk_profiles(client)
            item = (bulk_profile_map or {}).get(symbol) or {}
            if not item:
                profile_call_count += 1
                profile_fallback_call_count += 1
                profile = fetch_profile(client, symbol)
                item = profile[0] if isinstance(profile, list) and profile else {}
            if item:
                row.update({
                    "companyName": item.get("companyName") or item.get("name") or row.get("companyName"),
                    "sector": item.get("sector"),
                    "marketCap": as_num(item.get("marketCap") or item.get("mktCap")),
                })
                if price <= 0:
                    row["price"] = price = as_num(item.get("price"))
                if avg_volume <= 0:
                    row["averageVolume"] = avg_volume = as_num(item.get("averageVolume") or item.get("avgVolume") or item.get("volume"))
                if volume <= 0:
                    row["volume"] = volume = as_num(item.get("volume"))
                row["dollarVolume"] = round(price * avg_volume, 2)

        if item:
            row.update({
                "companyName": item.get("companyName") or item.get("name") or row.get("companyName"),
                "sector": item.get("sector") or row.get("sector"),
                "marketCap": as_num(item.get("marketCap") or item.get("mktCap"), row.get("marketCap") or 0),
            })
            if (item.get("isEtf") or item.get("isFund")) and "fund_or_etf" not in row["rejectReasons"]:
                row["rejectReasons"].append("fund_or_etf")
            if item.get("isActivelyTrading") is False and "inactive" not in row["rejectReasons"]:
                row["rejectReasons"].append("inactive")

        if not row["rejectReasons"]:
            row["status"] = "PASS"
            survivors.append(row)
        details.append(row)

    print(
        f"B2 progress: {total_candidates}/{total_candidates} (100%) kept={len(survivors)} "
        f"fmp_calls={client.calls} fmp_errors={len(client.errors)}",
        flush=True,
    )

    reject_counts: dict[str, int] = {}
    for row in details:
        for reason in row.get("rejectReasons", []):
            reject_counts[reason] = reject_counts.get(reason, 0) + 1

    rejection_breakdown = {
        "removed_volume": reject_counts.get("avg_volume_below_1m", 0),
        "removed_price": reject_counts.get("price_below_5", 0),
        "removed_dollar_vol": reject_counts.get("dollar_volume_below_20m", 0),
        "removed_earnings": sum(count for reason, count in reject_counts.items() if str(reason).startswith("earnings_blackout")),
        "removed_other": sum(
            count
            for reason, count in reject_counts.items()
            if reason not in {"avg_volume_below_1m", "price_below_5", "dollar_volume_below_20m"}
            and not str(reason).startswith("earnings_blackout")
        ),
    }
    exclusive_breakdown = {key: 0 for key in rejection_breakdown}
    for row in details:
        if row.get("status") == "PASS":
            continue
        reasons = row.get("rejectReasons") or []
        if "avg_volume_below_1m" in reasons:
            exclusive_breakdown["removed_volume"] += 1
        elif "price_below_5" in reasons:
            exclusive_breakdown["removed_price"] += 1
        elif "dollar_volume_below_20m" in reasons:
            exclusive_breakdown["removed_dollar_vol"] += 1
        elif any(str(reason).startswith("earnings_blackout") for reason in reasons):
            exclusive_breakdown["removed_earnings"] += 1
        else:
            exclusive_breakdown["removed_other"] += 1

    metadata = {
        "stage": "B2",
        "inputFile": input_name,
        "inputUniverseCount": len(candidates),
        "catalystTaggedInputCount": sum(1 for c in candidates if c.get("source") == "catalyst"),
        "survivorCount": len(survivors),
        "catalystTaggedSurvivorCount": sum(1 for s in survivors if s.get("source") == "catalyst"),
        "rejectedCount": len(candidates) - len(survivors),
        "reentryBlockedCount": sum(bool(row.get("reentryBlocked")) for row in details),
        "reentryBlockedSymbols": [
            row.get("symbol") for row in details if row.get("reentryBlocked")
        ],
        "profileCallCount": profile_call_count,
        "bulkProfileCallCount": bulk_profile_call_count,
        "profileFallbackCallCount": profile_fallback_call_count,
        "bulkProfileRows": len(bulk_profile_map or {}),
        "batchQuoteCallCount": quote_call_count,
        "globalEarningsCalendarCalls": 1,
        "totalFmpCalls": client.calls,
        "fmpErrorCount": len(client.errors),
        "fmpErrorsSample": client.errors[:20],
        "fmpCircuitOpen": client.circuit_open,
        "fmpConsecutiveFailures": client.consecutive_failures,
        "rejectCounts": reject_counts,
        "rejectReasonCounts": reject_counts,
        "rejectionBreakdown": exclusive_breakdown,
        "config": {
            "minAvgVolume": MIN_AVG_VOLUME,
            "minPrice": MIN_PRICE,
            "minDollarVolume": MIN_DOLLAR_VOLUME,
            "earningsBlackoutDays": EARNINGS_BLACKOUT_DAYS,
            "blockedTickers": sorted(BLOCKED_TICKERS),
        },
        "runtimeSeconds": round(time.time() - started, 2),
    }

    SURVIVORS_PATH.write_text(json.dumps(survivors, indent=2), encoding="utf-8")
    DETAILS_PATH.write_text(json.dumps(details, indent=2), encoding="utf-8")
    META_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
