#!/usr/bin/env python3
"""Standalone intraday bar recorder for future read-only strategy testing.

Additive data collection only. This script does not write fund.json, does not call
PMF setters, and does not interact with auto-execution or selection/scoring/gates.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "intraday_bars"
LOG_PATH = ROOT / "logs" / "intraday_bar_recorder.log"
FUND_PATH = Path("/root/fund-system/data/fund.json")
FINALIST_PATHS = [
    ROOT / "stage7_clustered_survivors.json",
    ROOT / "stage2_confluence_ranked_top40.json",
    ROOT / "stage2_surgical_strike_top40.json",
]
ENV_PATH = ROOT / ".env"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
ALPACA_DATA_BASE = "https://data.alpaca.markets/v2"
FMP_BASE = "https://financialmodelingprep.com"
DEFAULT_WINDOW_MINUTES = 120
DEFAULT_TIMEFRAME = "5Min"


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(UTC).isoformat()} {msg}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip('"\''))
    return env


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def target_session_bounds(day: date, window_minutes: int) -> tuple[datetime, datetime]:
    start_et = datetime.combine(day, dtime(9, 30), ET)
    end_et = start_et + timedelta(minutes=window_minutes)
    return start_et.astimezone(UTC), end_et.astimezone(UTC)


def ticker_of(row: dict[str, Any]) -> str:
    return str(row.get("ticker") or row.get("symbol") or "").upper().strip()


def collect_candidates(day: date) -> dict[str, set[str]]:
    reasons: dict[str, set[str]] = defaultdict(set)

    for path in FINALIST_PATHS:
        rows = read_json(path, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = ticker_of(row)
            if sym:
                reasons[sym].add(f"finalist:{path.name}")

    fund = read_json(FUND_PATH, {})
    for row in fund.get("ideas", []) if isinstance(fund, dict) else []:
        if not isinstance(row, dict):
            continue
        sym = ticker_of(row)
        if not sym:
            continue
        checked_day = parse_date(row.get("pre_market_checked_at")) or parse_date(row.get("date"))
        if checked_day != day:
            continue
        if row.get("pre_market_gap_favourable") is True or row.get("pre_market_favourable") is True:
            reasons[sym].add("gap_up:pre_market_gap_favourable")
        if row.get("pre_market_gap_adverse") is True:
            reasons[sym].add("gap_down:pre_market_gap_adverse")

    return reasons


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> tuple[Any, int, int]:
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json", "User-Agent": "system2-intraday-recorder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        status = getattr(resp, "status", 200)
    return json.loads(raw.decode("utf-8", "ignore") or "null"), status, len(raw)


def alpaca_headers(env: dict[str, str]) -> dict[str, str]:
    key = env.get("ALPACA_PAPER_API_KEY")
    secret = env.get("ALPACA_PAPER_API_SECRET")
    if not key or not secret:
        raise RuntimeError("Missing ALPACA_PAPER_API_KEY / ALPACA_PAPER_API_SECRET")
    return {
        "Accept": "application/json",
        "User-Agent": "system2-intraday-recorder/1.0",
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def normalize_alpaca_bar(bar: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": bar.get("t"),
        "open": bar.get("o"),
        "high": bar.get("h"),
        "low": bar.get("l"),
        "close": bar.get("c"),
        "volume": bar.get("v"),
        "source_vwap": bar.get("vw"),
        "trade_count": bar.get("n"),
    }


def normalize_fmp_bar(bar: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": bar.get("date"),
        "open": bar.get("open"),
        "high": bar.get("high"),
        "low": bar.get("low"),
        "close": bar.get("close"),
        "volume": bar.get("volume"),
        "source_vwap": bar.get("vwap"),
        "trade_count": None,
    }


def running_vwap(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cum_pv = 0.0
    cum_v = 0.0
    out: list[dict[str, Any]] = []
    for bar in bars:
        row = dict(bar)
        try:
            high = float(row.get("high") or 0)
            low = float(row.get("low") or 0)
            close = float(row.get("close") or 0)
            volume = float(row.get("volume") or 0)
            typical = (high + low + close) / 3.0
            cum_pv += typical * volume
            cum_v += volume
            row["running_vwap"] = round(cum_pv / cum_v, 6) if cum_v else None
        except Exception:
            row["running_vwap"] = None
        out.append(row)
    return out


def fetch_alpaca_bars(symbol: str, start: datetime, end: datetime, env: dict[str, str], timeframe: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = urllib.parse.urlencode({
        "timeframe": timeframe,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "feed": "iex",
        "adjustment": "raw",
        "limit": "1000",
    })
    url = f"{ALPACA_DATA_BASE}/stocks/{urllib.parse.quote(symbol)}/bars?{params}"
    data, status, nbytes = http_json(url, alpaca_headers(env))
    bars = data.get("bars", []) if isinstance(data, dict) else []
    return [normalize_alpaca_bar(b) for b in bars if isinstance(b, dict)], {"status": status, "bytes": nbytes, "url_kind": "alpaca_bars"}


def fetch_alpaca_prior_close(symbol: str, start: datetime, env: dict[str, str]) -> tuple[float | None, dict[str, Any]]:
    lookback_start = start - timedelta(days=10)
    params = urllib.parse.urlencode({
        "timeframe": "1Day",
        "start": lookback_start.isoformat().replace("+00:00", "Z"),
        "end": start.isoformat().replace("+00:00", "Z"),
        "feed": "iex",
        "adjustment": "raw",
        "limit": "10",
    })
    url = f"{ALPACA_DATA_BASE}/stocks/{urllib.parse.quote(symbol)}/bars?{params}"
    data, status, nbytes = http_json(url, alpaca_headers(env))
    bars = data.get("bars", []) if isinstance(data, dict) else []
    close = bars[-1].get("c") if bars else None
    return close, {"status": status, "bytes": nbytes, "url_kind": "alpaca_daily"}


def fetch_fmp_bars(symbol: str, day: date, env: dict[str, str], timeframe: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = env.get("FMP_API_KEY") or env.get("FMP_KEY")
    if not key:
        raise RuntimeError("Missing FMP_API_KEY fallback")
    interval = "5min" if timeframe.lower().startswith("5") else "1min"
    params = urllib.parse.urlencode({"symbol": symbol, "apikey": key})
    url = f"{FMP_BASE}/stable/historical-chart/{interval}?{params}"
    data, status, nbytes = http_json(url)
    rows = data if isinstance(data, list) else []
    wanted = str(day)
    normalized = [normalize_fmp_bar(b) for b in reversed(rows) if isinstance(b, dict) and str(b.get("date", "")).startswith(wanted)]
    return normalized, {"status": status, "bytes": nbytes, "url_kind": "fmp_bars"}


def record_symbol(symbol: str, reasons: set[str], day: date, env: dict[str, str], window_minutes: int, timeframe: str) -> dict[str, Any]:
    start, end = target_session_bounds(day, window_minutes)
    calls: list[dict[str, Any]] = []
    fallback_used = False
    source = "alpaca"
    error = None
    bars: list[dict[str, Any]] = []
    prior_close = None

    try:
        bars, meta = fetch_alpaca_bars(symbol, start, end, env, timeframe)
        calls.append(meta)
    except Exception as exc:
        error = f"alpaca_bars: {exc}"
        bars = []

    if not bars:
        try:
            fallback_used = True
            source = "fmp_fallback"
            bars, meta = fetch_fmp_bars(symbol, day, env, timeframe)
            calls.append(meta)
        except Exception as exc:
            error = f"{error}; fmp_fallback: {exc}" if error else f"fmp_fallback: {exc}"
            bars = []

    try:
        prior_close, meta = fetch_alpaca_prior_close(symbol, start, env)
        calls.append(meta)
    except Exception as exc:
        error = f"{error}; alpaca_prior_close: {exc}" if error else f"alpaca_prior_close: {exc}"

    bars = running_vwap(bars)
    day_open = bars[0].get("open") if bars else None
    payload = {
        "symbol": symbol,
        "date": str(day),
        "timeframe": timeframe,
        "window_minutes": window_minutes,
        "session_start_utc": start.isoformat(),
        "session_end_utc": end.isoformat(),
        "source": source,
        "fallback_used": fallback_used,
        "eligibility_reasons": sorted(reasons),
        "prior_close": prior_close,
        "day_open": day_open,
        "bar_count": len(bars),
        "bars": bars,
        "call_observations": calls,
        "error": error,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    out_path = DATA_DIR / str(day) / f"{symbol}.json"
    write_json(out_path, payload)
    return {
        "symbol": symbol,
        "path": str(out_path),
        "source": source,
        "fallback_used": fallback_used,
        "bar_count": len(bars),
        "bytes": sum(int(c.get("bytes") or 0) for c in calls),
        "calls": len(calls),
        "error": error,
        "gap_down": any(str(r).startswith("gap_down:") for r in reasons),
        "gap_up": any(str(r).startswith("gap_up:") for r in reasons),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Record first-session intraday bars for finalists and gap up/down names.")
    parser.add_argument("--date", help="ET session date YYYY-MM-DD; default today in America/New_York")
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME, choices=["5Min", "1Min"])
    parser.add_argument("--max-tickers", type=int, default=0, help="optional sample cap; 0 means all")
    parser.add_argument("--include", action="append", default=[], help="force-include ticker, repeatable")
    args = parser.parse_args()

    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(ET).date()
    env = load_env()
    candidates = collect_candidates(day)
    for sym in args.include:
        if sym:
            candidates[str(sym).upper()].add("manual_include")
    symbols = sorted(candidates)
    if args.max_tickers and args.max_tickers > 0:
        gap_down = [s for s in symbols if any(r.startswith("gap_down:") for r in candidates[s])]
        rest = [s for s in symbols if s not in gap_down]
        symbols = (gap_down + rest)[: args.max_tickers]

    summary = {
        "ok": True,
        "date": str(day),
        "candidate_count": len(candidates),
        "recorded_count": 0,
        "gap_down_candidate_count": sum(1 for s in candidates if any(r.startswith("gap_down:") for r in candidates[s])),
        "gap_up_candidate_count": sum(1 for s in candidates if any(r.startswith("gap_up:") for r in candidates[s])),
        "selected_count": len(symbols),
        "alpaca_primary_count": 0,
        "fmp_fallback_count": 0,
        "total_calls_observed": 0,
        "total_bytes_observed": 0,
        "errors": [],
        "rows": [],
        "storage_dir": str(DATA_DIR / str(day)),
    }

    for sym in symbols:
        try:
            row = record_symbol(sym, candidates[sym], day, env, args.window_minutes, args.timeframe)
            summary["rows"].append(row)
            summary["recorded_count"] += 1
            summary["total_calls_observed"] += row["calls"]
            summary["total_bytes_observed"] += row["bytes"]
            if row["fallback_used"]:
                summary["fmp_fallback_count"] += 1
                log(f"FMP_FALLBACK symbol={sym} bars={row['bar_count']} error={row.get('error')}")
            else:
                summary["alpaca_primary_count"] += 1
        except Exception as exc:
            msg = f"{sym}: {exc}"
            summary["errors"].append(msg)
            log(f"ERROR {msg}")
        time.sleep(0.05)

    summary_path = DATA_DIR / str(day) / "_summary.json"
    write_json(summary_path, summary)
    log(
        "SUMMARY "
        + json.dumps({k: summary[k] for k in ["date", "candidate_count", "selected_count", "recorded_count", "gap_down_candidate_count", "gap_up_candidate_count", "alpaca_primary_count", "fmp_fallback_count", "total_calls_observed", "total_bytes_observed"]}, sort_keys=True)
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not summary["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
