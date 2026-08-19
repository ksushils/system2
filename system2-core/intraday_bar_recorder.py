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


SNAPSHOT_TIMES = ("09:35", "09:45", "10:00", "10:30")


def bar_start_et(bar: dict[str, Any]) -> datetime | None:
    raw = bar.get("timestamp")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except Exception:
        return None


def completed_bars(bars: list[dict[str, Any]], day: date, boundary: str) -> list[dict[str, Any]]:
    hour, minute = (int(part) for part in boundary.split(":"))
    cutoff = datetime.combine(day, dtime(hour, minute), ET)
    return [bar for bar in bars if (bar_start_et(bar) is not None and bar_start_et(bar) + timedelta(minutes=5) <= cutoff)]


def cumulative_volume(bars: list[dict[str, Any]], day: date, boundary: str) -> float | None:
    selected = completed_bars(bars, day, boundary)
    values = [float(bar.get("volume")) for bar in selected if num_safe(bar.get("volume")) is not None]
    return sum(values) if values else None


def num_safe(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else None
    except Exception:
        return None


def prior_cumulative_baseline(symbol: str, day: date, boundary: str) -> float | None:
    values: list[float] = []
    for directory in sorted(DATA_DIR.iterdir(), reverse=True) if DATA_DIR.exists() else []:
        prior_day = parse_date(directory.name)
        if prior_day is None or prior_day >= day:
            continue
        payload = read_json(directory / f"{symbol}.json", {})
        volume = cumulative_volume(payload.get("bars", []), prior_day, boundary) if isinstance(payload, dict) else None
        if volume is not None and volume > 0:
            values.append(volume)
        if len(values) == 20:
            break
    return sum(values) / 20 if len(values) == 20 else None


def derive_opening_snapshots(symbol: str, day: date, bars: list[dict[str, Any]]) -> dict[str, Any]:
    regular = [bar for bar in bars if bar_start_et(bar) and bar_start_et(bar).date() == day and bar_start_et(bar).time() >= dtime(9, 30)]
    day_open = num_safe(regular[0].get("open")) if regular else None
    opening_range_bars = [bar for bar in regular if dtime(9, 30) <= bar_start_et(bar).time() < dtime(9, 45)]
    range_highs = [num_safe(bar.get("high")) for bar in opening_range_bars]
    range_lows = [num_safe(bar.get("low")) for bar in opening_range_bars]
    range_high = max(value for value in range_highs if value is not None) if any(value is not None for value in range_highs) else None
    range_low = min(value for value in range_lows if value is not None) if any(value is not None for value in range_lows) else None
    premarket_highs = [num_safe(bar.get("high")) for bar in bars if bar_start_et(bar) and bar_start_et(bar).date() == day and bar_start_et(bar).time() < dtime(9, 30)]
    premarket_high = max(value for value in premarket_highs if value is not None) if any(value is not None for value in premarket_highs) else None
    snapshots: dict[str, Any] = {}
    for boundary in SNAPSHOT_TIMES:
        selected = completed_bars(bars, day, boundary)
        last = selected[-1] if selected else None
        close = num_safe(last.get("close")) if last else None
        vwap = num_safe(last.get("running_vwap")) if last else None
        today_volume = cumulative_volume(bars, day, boundary)
        baseline = prior_cumulative_baseline(symbol, day, boundary)
        range_state = None
        if boundary != "09:35" and close is not None and range_high is not None and range_low is not None:
            range_state = "ABOVE" if close > range_high else ("BELOW" if close < range_low else "INSIDE")
        snapshots[boundary] = {
            "change_from_open_pct": round((close - day_open) / day_open * 100, 4) if close is not None and day_open else None,
            "rvol_time": round(today_volume / baseline, 4) if today_volume is not None and baseline else None,
            "above_vwap": close > vwap if close is not None and vwap is not None else None,
            "opening_range_state": range_state,
            "premarket_high_dist_pct": round((close - premarket_high) / premarket_high * 100, 4) if close is not None and premarket_high else None,
        }
    return snapshots


def idea_geometry(row: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    entry = num_safe(row.get("entry_trigger_price") or row.get("modeled_entry") or row.get("entry"))
    stop = num_safe(row.get("original_stop") or row.get("stop"))
    target = num_safe(row.get("original_target") or row.get("target"))
    return entry, stop, target


def first_touch_labels(symbol: str, day: date, bars: list[dict[str, Any]], fund: dict[str, Any]) -> list[dict[str, Any]]:
    labels = []
    for row in fund.get("ideas", []) if isinstance(fund, dict) else []:
        if not isinstance(row, dict) or ticker_of(row) != symbol:
            continue
        row_day = parse_date(row.get("pmf_stamp_time")) or parse_date(row.get("pre_market_checked_at")) or parse_date(row.get("date"))
        if row_day != day:
            continue
        entry, stop, target = idea_geometry(row)
        if entry is None or stop is None or target is None:
            continue
        is_short = target < entry
        entered = False
        outcome = "NEITHER"
        touch_at = None
        ambiguous = False
        for bar in bars:
            high, low = num_safe(bar.get("high")), num_safe(bar.get("low"))
            if high is None or low is None:
                continue
            if not entered:
                entered = low <= entry <= high
                if not entered:
                    continue
            target_hit = low <= target if is_short else high >= target
            stop_hit = high >= stop if is_short else low <= stop
            if target_hit and stop_hit:
                outcome, ambiguous, touch_at = "STOP_FIRST", True, bar.get("timestamp")
                break
            if stop_hit:
                outcome, touch_at = "STOP_FIRST", bar.get("timestamp")
                break
            if target_hit:
                outcome, touch_at = "TARGET_FIRST", bar.get("timestamp")
                break
        labels.append({
            "idea_id": row.get("id"), "ticker": symbol, "entry": entry, "stop": stop, "target": target,
            "first_touch": outcome, "first_touch_at": touch_at, "first_touch_same_bar_ambiguous": ambiguous,
            "first_touch_source": "stored_5min_bars", "first_touch_version": 1,
        })
    return labels


def derive_payload(payload: dict[str, Any], fund: dict[str, Any]) -> dict[str, Any]:
    symbol = str(payload.get("symbol") or "").upper()
    day = parse_date(payload.get("date"))
    bars = payload.get("bars", [])
    if not symbol or day is None or not isinstance(bars, list):
        return payload
    payload["opening_snapshots"] = derive_opening_snapshots(symbol, day, bars)
    payload["opening_snapshot_version"] = 1
    payload["opening_snapshot_derived_at"] = datetime.now(UTC).isoformat()
    payload["opening_snapshot_source"] = "stored_5min_bars"
    payload["first_touch_labels"] = first_touch_labels(symbol, day, bars, fund)
    return payload


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


def record_symbol(symbol: str, reasons: set[str], day: date, env: dict[str, str], window_minutes: int, timeframe: str, fund: dict[str, Any]) -> dict[str, Any]:
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
    payload = derive_payload(payload, fund)
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
    parser.add_argument("--derive-existing", action="store_true", help="derive snapshots/labels from stored files only; makes zero API calls")
    args = parser.parse_args()

    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(ET).date()
    fund = read_json(FUND_PATH, {})
    if args.derive_existing:
        selected_dirs = [DATA_DIR / str(day)] if args.date else sorted(path for path in DATA_DIR.iterdir() if path.is_dir())
        files = [path for directory in selected_dirs for path in directory.glob("*.json") if path.name != "_summary.json"]
        before_bytes = sum(path.stat().st_size for path in files)
        ambiguous = 0
        label_count = 0
        for file_path in files:
            payload = read_json(file_path, {})
            if not isinstance(payload, dict):
                continue
            derived = derive_payload(payload, fund)
            labels = derived.get("first_touch_labels", [])
            label_count += len(labels)
            ambiguous += sum(row.get("first_touch_same_bar_ambiguous") is True for row in labels)
            write_json(file_path, derived)
        after_bytes = sum(path.stat().st_size for path in files)
        print(json.dumps({"ok": True, "mode": "derive_existing", "files": len(files), "first_touch_labels": label_count, "same_bar_ambiguous": ambiguous, "before_bytes": before_bytes, "after_bytes": after_bytes, "added_bytes": after_bytes - before_bytes, "additional_api_calls": 0}, indent=2, sort_keys=True))
        return 0
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
            row = record_symbol(sym, candidates[sym], day, env, args.window_minutes, args.timeframe, fund)
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
