#!/usr/bin/env python3
"""Strict PEAD_DRIFT forward paper tracker.

Additive and separate from System 2 PMF/scoring. By default this script is
disabled and dry-run oriented. It only writes to fund.json when
PEAD_DRIFT_ENABLED=true and --write is passed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
FUND_PATH = Path("/root/fund-system/data/fund.json")
FMP_BASE = "https://financialmodelingprep.com"


class Metrics:
    def __init__(self) -> None:
        self.fmp_calls = 0
        self.fmp_bytes_observed = 0
        self.cache_hits = 0


METRICS = Metrics()


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()
PEAD_DRIFT_ENABLED = os.getenv("PEAD_DRIFT_ENABLED", "false").strip().lower() == "true"
PEAD_SURPRISE_MIN_PCT = float(os.getenv("PEAD_SURPRISE_MIN_PCT", "52.54"))
PEAD_ENTRY_SLIPPAGE_PCT = float(os.getenv("PEAD_ENTRY_SLIPPAGE_PCT", "0.001"))
PEAD_STOP_ATR_MULTIPLE = float(os.getenv("PEAD_STOP_ATR_MULTIPLE", "2.0"))
PEAD_HOLD_TRADING_DAYS = int(os.getenv("PEAD_HOLD_TRADING_DAYS", "60"))


def fmp_key() -> str:
    load_dotenv()
    key = os.getenv("FMP_API_KEY") or os.getenv("FMP_KEY")
    if not key:
        raise RuntimeError("FMP_API_KEY not configured")
    return key


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def eastern_session_fields(timestamp: datetime) -> dict[str, Any]:
    eastern = timestamp.astimezone(ZoneInfo("America/New_York"))
    local_minutes = eastern.hour * 60 + eastern.minute
    minutes_from_open = local_minutes - (9 * 60 + 30)
    state = "PREMARKET" if local_minutes < 9 * 60 + 30 else ("REGULAR" if local_minutes < 16 * 60 else "AFTERHOURS")
    return {
        "session_eastern_time": eastern.isoformat(),
        "session_state": state,
        "minutes_from_open": minutes_from_open,
        "session_label": "PREOPEN_PMF" if minutes_from_open < 0 else "POSTOPEN_PMF",
    }


def num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        v = float(value)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def today_cache_path(endpoint: str, params: dict[str, Any]) -> Path:
    import hashlib
    import re

    day = utc_now().date().isoformat()
    normalized = json.dumps({"endpoint": endpoint, "params": params}, sort_keys=True, separators=(",", ":"))
    readable = re.sub(r"[^A-Za-z0-9._=-]+", "_", normalized).strip("_")[:120]
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return ROOT / "data" / "fmp_cache" / day / f"{readable}-{digest}.json"


def fmp_get(endpoint: str, params: dict[str, Any] | None = None) -> Any:
    params = params or {}
    cache_path = today_cache_path(endpoint, params)
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("date") == utc_now().date().isoformat():
                METRICS.cache_hits += 1
                return payload.get("data")
        except Exception:
            pass

    query = urllib.parse.urlencode({**params, "apikey": fmp_key()})
    url = f"{FMP_BASE}/{endpoint}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "system2-pead-drift/1.0"})
    raw = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
            time.sleep(min(delay, 30))
    if raw is None:
        raise RuntimeError("FMP request produced no response")
    METRICS.fmp_calls += 1
    METRICS.fmp_bytes_observed += len(raw)
    data = json.loads(raw.decode("utf-8", "ignore"))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "date": utc_now().date().isoformat(),
        "createdAt": utc_now().isoformat(),
        "endpoint": endpoint,
        "params": params,
        "data": data,
    }, indent=2), encoding="utf-8")
    return data


def parse_date(value: Any) -> datetime | None:
    raw = str(value or "")[:10]
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def tracked_symbols() -> list[str]:
    symbols: list[str] = []
    for path in [
        ROOT / "stage2_confluence_ranked_top40.json",
        ROOT / "stage2_surgical_strike_top40.json",
        ROOT / "stage6_council_enriched.json",
        ROOT / "stage7_finalists.json",
    ]:
        data = load_json(path, [])
        rows = data if isinstance(data, list) else data.get("ideas", data.get("finalists", [])) if isinstance(data, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("ticker") or row.get("symbol") or "").upper().strip()
            if sym and sym not in symbols:
                symbols.append(sym)
    drift = load_json(ROOT / "data" / "earnings_drift.json", {})
    for sym in (drift.get("tickers") or {}).keys() if isinstance(drift, dict) else []:
        sym = str(sym).upper().strip()
        if sym and sym not in symbols:
            symbols.append(sym)
    return symbols[:120]


def earnings_rows(symbols: list[str], lookback_days: int) -> list[dict[str, Any]]:
    since = utc_now().date() - timedelta(days=lookback_days)
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        try:
            data = fmp_get("stable/earnings-calendar", {"symbol": sym})
        except Exception:
            data = []
        if not isinstance(data, list):
            continue
        for row in data:
            if str(row.get("symbol") or "").upper() != sym:
                continue
            dt = parse_date(row.get("date"))
            if not dt or dt.date() < since or dt.date() > utc_now().date():
                continue
            actual = num(row.get("epsActual"))
            estimate = num(row.get("epsEstimated"))
            if actual is None or estimate in (None, 0):
                continue
            surprise_pct = (actual - estimate) / abs(estimate) * 100
            rows.append({
                "ticker": sym,
                "earnings_date": dt.date().isoformat(),
                "actual_eps": actual,
                "estimated_eps": estimate,
                "earnings_surprise_pct": round(surprise_pct, 4),
                "earnings_source": "fmp_earnings_calendar",
            })
        time.sleep(0.05)
    return rows


def daily_bars(symbol: str) -> list[dict[str, Any]]:
    data = fmp_get("stable/historical-price-eod/full", {"symbol": symbol})
    rows = data.get("historical") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        dt = parse_date(row.get("date"))
        close = num(row.get("close"))
        high = num(row.get("high"))
        low = num(row.get("low"))
        open_ = num(row.get("open"))
        if dt and close is not None and high is not None and low is not None:
            out.append({"date": dt.date().isoformat(), "open": open_, "high": high, "low": low, "close": close})
    return sorted({r["date"]: r for r in out}.values(), key=lambda r: r["date"])


def atr14(bars: list[dict[str, Any]], idx: int) -> float | None:
    if idx < 15:
        return None
    trs = []
    for i in range(idx - 14, idx):
        h = num(bars[i].get("high"))
        l = num(bars[i].get("low"))
        pc = num(bars[i - 1].get("close"))
        if h is None or l is None or pc is None:
            return None
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


def r_value(entry: float, risk: float, price: float | None) -> float | None:
    if price is None or risk <= 0:
        return None
    return round((price - entry) / risk, 4)


def build_candidate(event: dict[str, Any]) -> dict[str, Any] | None:
    if num(event.get("earnings_surprise_pct")) is None or float(event["earnings_surprise_pct"]) < PEAD_SURPRISE_MIN_PCT:
        return None
    bars = daily_bars(event["ticker"])
    if not bars:
        return None
    idx = next((i for i, bar in enumerate(bars) if bar["date"] >= event["earnings_date"]), None)
    if idx is None or idx == 0:
        return None
    prior_close = num(bars[idx - 1].get("close"))
    reaction_close = num(bars[idx].get("close"))
    atr = atr14(bars, idx)
    if prior_close is None or reaction_close is None or atr is None or atr <= 0:
        return None
    reaction_return_pct = (reaction_close - prior_close) / prior_close * 100
    if reaction_close <= prior_close:
        return None
    entry = reaction_close * (1 + PEAD_ENTRY_SLIPPAGE_PCT)
    risk = PEAD_STOP_ATR_MULTIPLE * atr
    stop = entry - risk
    logged_at = utc_now()
    return {
        "id": f"PEAD_DRIFT_{event['earnings_date']}_{event['ticker']}",
        "strategy": "PEAD_DRIFT",
        "paper_only": True,
        "ticker": event["ticker"],
        "logged_at": logged_at.isoformat(),
        **eastern_session_fields(logged_at),
        "earnings_date": event["earnings_date"],
        "date": event["earnings_date"],
        "actual_eps": event["actual_eps"],
        "estimated_eps": event["estimated_eps"],
        "earnings_surprise_pct": event["earnings_surprise_pct"],
        "earnings_source": event["earnings_source"],
        "prior_close": round(prior_close, 4),
        "reaction_close": round(reaction_close, 4),
        "reaction_return_pct": round(reaction_return_pct, 4),
        "atr14": round(atr, 4),
        "modeled_entry": round(entry, 4),
        "entry": round(entry, 4),
        "modeled_entry_slippage_pct": PEAD_ENTRY_SLIPPAGE_PCT,
        "stop": round(stop, 4),
        "risk_per_share": round(risk, 4),
        "stop_atr_multiple": PEAD_STOP_ATR_MULTIPLE,
        "hold_trading_days": PEAD_HOLD_TRADING_DAYS,
        "paper_status": "OPEN",
        "canonical_r": None,
        "markout_r_current": r_value(entry, risk, reaction_close),
        "markout_r_10d": None,
        "markout_r_20d": None,
        "markout_r_40d": None,
        "markout_r_60d": None,
        "paper_exit_reason": None,
        "paper_exit_at": None,
        "paper_exit_price": None,
        "resolved_at": None,
        "notes": "PAPER ONLY. Low win-rate, fat-tail PEAD drift cohort. Separate from PMF.",
    }


def update_open_row(row: dict[str, Any]) -> bool:
    if row.get("paper_status") != "OPEN":
        return False
    bars = daily_bars(str(row.get("ticker", "")).upper())
    if not bars:
        return False
    start_idx = next((i for i, bar in enumerate(bars) if bar["date"] >= row.get("earnings_date")), None)
    if start_idx is None:
        return False
    entry = num(row.get("modeled_entry") or row.get("entry"))
    risk = num(row.get("risk_per_share"))
    stop = num(row.get("stop"))
    if entry is None or risk is None or risk <= 0 or stop is None:
        return False
    after = bars[start_idx + 1:]
    changed = False
    latest_close = num(bars[-1].get("close")) if bars else None
    refreshed_at = utc_now().isoformat()
    refreshed_r = r_value(entry, risk, latest_close)
    if row.get("markout_r_current") != refreshed_r or row.get("markout_refreshed_at") is None:
        row["markout_r_current"] = refreshed_r
        row["markout_refreshed_at"] = refreshed_at
        changed = True
    for days in (10, 20, 40, 60):
        key = f"markout_r_{days}d"
        if len(after) >= days and row.get(key) is None:
            row[key] = r_value(entry, risk, num(after[days - 1].get("close")))
            changed = True
    for idx, bar in enumerate(after[:PEAD_HOLD_TRADING_DAYS], start=1):
        low = num(bar.get("low"))
        if low is not None and low <= stop:
            row["paper_status"] = "RESOLVED"
            row["paper_exit_reason"] = "STOP"
            row["paper_exit_at"] = bar["date"]
            row["paper_exit_price"] = round(stop, 4)
            row["canonical_r"] = -1.0
            row["resolved_at"] = utc_now().isoformat()
            return True
    if len(after) >= PEAD_HOLD_TRADING_DAYS:
        exit_bar = after[PEAD_HOLD_TRADING_DAYS - 1]
        exit_price = num(exit_bar.get("close"))
        row["paper_status"] = "RESOLVED"
        row["paper_exit_reason"] = "TIME_60D"
        row["paper_exit_at"] = exit_bar["date"]
        row["paper_exit_price"] = round(exit_price, 4) if exit_price is not None else None
        row["canonical_r"] = r_value(entry, risk, exit_price)
        row["resolved_at"] = utc_now().isoformat()
        return True
    return changed


def run(write: bool, lookback_days: int) -> dict[str, Any]:
    fund = load_json(FUND_PATH, {})
    rows = fund.setdefault("pead_drift_paper", [])
    existing = {str(r.get("id")) for r in rows if isinstance(r, dict)}
    events = earnings_rows(tracked_symbols(), lookback_days)
    candidates = []
    for event in events:
        candidate = build_candidate(event)
        if candidate and candidate["id"] not in existing:
            candidates.append(candidate)

    updated = 0
    if write and PEAD_DRIFT_ENABLED:
        rows.extend(candidates)
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                if update_open_row(row):
                    updated += 1
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
        body = json.dumps({"rows": rows}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:3210/api/system2/pead-drift/upsert-local",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            persistence = json.loads(resp.read().decode("utf-8", "ignore"))
        if not persistence.get("ok"):
            raise RuntimeError(f"PEAD persistence failed: {persistence}")

    return {
        "ok": True,
        "enabled": PEAD_DRIFT_ENABLED,
        "write_requested": write,
        "wrote": bool(write and PEAD_DRIFT_ENABLED),
        "lookback_days": lookback_days,
        "events_checked": len(events),
        "would_log_count": len(candidates),
        "logged_count": len(candidates) if write and PEAD_DRIFT_ENABLED else 0,
        "updated_open_count": updated,
        "fmp_calls": METRICS.fmp_calls,
        "fmp_bytes_observed": METRICS.fmp_bytes_observed,
        "cache_hits": METRICS.cache_hits,
        "candidates": candidates,
        "note": "PEAD_DRIFT remains disabled unless PEAD_DRIFT_ENABLED=true and --write is passed.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Persist only when PEAD_DRIFT_ENABLED=true")
    parser.add_argument("--lookback-days", type=int, default=7)
    args = parser.parse_args()
    print(json.dumps(run(write=args.write, lookback_days=args.lookback_days), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
