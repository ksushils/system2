#!/usr/bin/env python3
"""System 2 date-range backtest engine.

Paper-mode only. Replays the current B3-style technical setup over a historical
date range using cached FMP daily OHLCV data, then simulates next-day entry with
ATR stops/targets.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any


SYSTEM2_ROOT = Path("/root/system2-core")
CACHE_DIR = SYSTEM2_ROOT / "backtest_cache"
DATA_DIR = SYSTEM2_ROOT / "data"
UNIVERSE_PATH = SYSTEM2_ROOT / "universe.json"
FMP_BASE = "https://financialmodelingprep.com"
HISTORY_BUFFER_DAYS = 80
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 500_000
FMP_DELAY = 0.08


def load_fmp_key() -> str:
    for key in ("FMP_API_KEY", "FMP_KEY"):
        value = os.environ.get(key)
        if value:
            return value.strip()
    env_path = SYSTEM2_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() in {"FMP_API_KEY", "FMP_KEY"}:
                    return value.strip().strip("\"'")
    raise RuntimeError("FMP API key not found. Set FMP_API_KEY.")


FMP_KEY = load_fmp_key()


def num(value: Any, default: float | None = None) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except Exception:
        return default


def fmp_get(endpoint: str, params: dict[str, Any] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    sep = "&" if "?" in endpoint else "?"
    url = f"{FMP_BASE}/{endpoint}{sep}apikey={urllib.parse.quote(FMP_KEY)}"
    if query:
        url += f"&{query}"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "system2-backtest/2.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            return {"error": f"HTTP {exc.code}", "body": exc.read().decode("utf-8", "ignore")[:200]}
        except Exception as exc:
            if attempt < 3:
                time.sleep(1 * (attempt + 1))
                continue
            return {"error": str(exc)}
    return {"error": "max retries exceeded"}


def fetch_ohlcv(ticker: str, start: str, end: str, run_cache: dict[str, list[dict]]) -> list[dict]:
    key = f"{ticker}_{start}_{end}"
    if key in run_cache:
        return run_cache[key]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        try:
            rows = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                run_cache[key] = rows
                return rows
        except Exception:
            pass

    endpoint = f"stable/historical-price-eod/full?symbol={urllib.parse.quote(ticker)}&from={start}&to={end}"
    raw = fmp_get(endpoint)
    rows = []
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("historical") or raw.get("data") or raw.get("results") or []

    out: list[dict] = []
    for row in rows:
        date = str(row.get("date") or "")[:10]
        o, h, l, c = num(row.get("open")), num(row.get("high")), num(row.get("low")), num(row.get("close"))
        v = num(row.get("volume"), 0)
        if not date or o is None or h is None or l is None or c is None:
            continue
        out.append({"date": date, "open": round(o, 4), "high": round(h, 4), "low": round(l, 4), "close": round(c, 4), "volume": int(v or 0)})
    out.sort(key=lambda r: r["date"])
    cache_file.write_text(json.dumps(out, indent=2), encoding="utf-8")
    run_cache[key] = out
    time.sleep(FMP_DELAY)
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)
    out: list[float | None] = [None] * (period - 1)
    cur = sum(values[:period]) / period
    out.append(cur)
    k = 2 / (period + 1)
    for value in values[period:]:
        cur = value * k + cur * (1 - k)
        out.append(cur)
    return out


def atr14(bars: list[dict]) -> float | None:
    if len(bars) < 15:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    cur = sum(trs[:14]) / 14
    for tr in trs[14:]:
        cur = (cur * 13 + tr) / 14
    return cur


def vwap20(bars: list[dict]) -> float | None:
    if len(bars) < 20:
        return None
    window = bars[-20:]
    vol = sum(b["volume"] for b in window)
    return sum(b["close"] * b["volume"] for b in window) / vol if vol > 0 else None


def mean_volume_20d(bars: list[dict]) -> float | None:
    if len(bars) < 20:
        return None
    return sum(b["volume"] for b in bars[-20:]) / 20


def compute_signals(
    bars: list[dict],
    spy_by_date: dict[str, dict],
    idx: int,
    setup_threshold: float,
    rvol_gate: float,
) -> dict[str, Any] | None:
    if idx < 50:
        return None
    window = bars[: idx + 1]
    bar = bars[idx]
    close = bar["close"]
    if close < MIN_PRICE:
        return None

    closes = [b["close"] for b in window]
    e8, e21, e50 = ema(closes, 8)[idx], ema(closes, 21)[idx], ema(closes, 50)[idx]
    bull_stack = bool(e8 is not None and e21 is not None and e50 is not None and close > e8 > e21 > e50)
    vwap = vwap20(window)
    vwap_pct = ((close - vwap) / vwap * 100) if vwap else 0.0
    avg_vol = mean_volume_20d(window)
    if avg_vol is not None and avg_vol < MIN_AVG_VOLUME:
        return None
    rvol = (bar["volume"] / avg_vol) if avg_vol and avg_vol > 0 else 1.0
    if rvol < rvol_gate:
        return None
    atr = atr14(window)
    if not atr:
        return None
    atr_pct = atr / close if close > 0 else 1.0

    rs_vs_spy = 0.0
    if idx >= 20:
        spy_now = spy_by_date.get(bar["date"])
        spy_then = spy_by_date.get(bars[idx - 20]["date"])
        stock_then = bars[idx - 20]["close"]
        if spy_now and spy_then and stock_then > 0 and spy_then["close"] > 0:
            rs_vs_spy = ((close / stock_then - 1) - (spy_now["close"] / spy_then["close"] - 1)) * 100

    score = 0
    score += 25 if rvol >= 3 else 15 if rvol >= 2 else 8 if rvol >= 1.5 else 0
    score += 20 if vwap_pct >= 1 else 10 if vwap_pct > 0 else 0
    score += 25 if bull_stack else 0
    score += 20 if rs_vs_spy >= 3 else 12 if rs_vs_spy >= 1 else 5 if rs_vs_spy > 0 else 0
    score += 10 if atr_pct < 0.025 else 0
    setup_score = max(0, min(100, round(score)))
    if setup_score < setup_threshold:
        return None

    return {
        "date": bar["date"],
        "close": close,
        "setup_score": setup_score,
        "setup_type": "BREAKOUT" if bull_stack and vwap_pct > 0 else "PULLBACK",
        "rvol": round(rvol, 2),
        "vwap_pct": round(vwap_pct, 2),
        "bull_ema_stack": bull_stack,
        "rs_vs_spy": round(rs_vs_spy, 2),
        "atr14": round(atr, 4),
    }


def simulate_trade(signal: dict[str, Any], future_bars: list[dict], hold_days: int, stop_atr: float, target_atr: float) -> dict[str, Any] | None:
    if not future_bars:
        return None
    entry_price = future_bars[0]["open"]
    atr = num(signal.get("atr14"))
    if not atr or atr <= 0:
        return None
    stop = entry_price - stop_atr * atr
    target = entry_price + target_atr * atr
    risk = entry_price - stop
    if risk <= 0:
        return None

    exit_price = future_bars[:hold_days][-1]["close"]
    exit_date = future_bars[:hold_days][-1]["date"]
    exit_reason = "TIMEOUT"
    hold_actual = min(hold_days, len(future_bars))
    for i, bar in enumerate(future_bars[:hold_days]):
        if bar["low"] <= stop:
            exit_price = stop
            exit_date = bar["date"]
            exit_reason = "STOP"
            hold_actual = i + 1
            break
        if bar["high"] >= target:
            exit_price = target
            exit_date = bar["date"]
            exit_reason = "TARGET"
            hold_actual = i + 1
            break

    r_multiple = (exit_price - entry_price) / risk
    return {
        "date": signal["date"],
        "ticker": signal["ticker"],
        "entry_price": round(entry_price, 4),
        "entry_date": future_bars[0]["date"],
        "stop": round(stop, 4),
        "target": round(target, 4),
        "exit_price": round(exit_price, 4),
        "exit_date": exit_date,
        "exit_reason": exit_reason,
        "r_multiple": round(r_multiple, 3),
        "hold_days_actual": hold_actual,
        "setup_score": signal["setup_score"],
        "setup_type": signal["setup_type"],
        "rvol": signal["rvol"],
        "regime": signal.get("regime", "NORMAL"),
    }


def substats(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0}
    vals = [t["r_multiple"] for t in trades]
    wins = [v for v in vals if v > 0]
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_r": round(mean(vals), 3),
        "total_r": round(sum(vals), 2),
    }


def build_results(params: dict[str, Any], trades: list[dict], result_path: str) -> dict[str, Any]:
    trades.sort(key=lambda t: (t["exit_date"], t["ticker"]))
    vals = [t["r_multiple"] for t in trades]
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    curve = []
    for t in trades:
        cum += t["r_multiple"]
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
        curve.append({"date": t["exit_date"], "cumulative_r": round(cum, 3), "drawdown_r": round(dd, 3)})

    stats = {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_r": round(mean(vals), 3) if vals else 0.0,
        "total_r": round(sum(vals), 2) if vals else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
        "best_r": round(max(vals), 3) if vals else 0.0,
        "worst_r": round(min(vals), 3) if vals else 0.0,
        "avg_hold_days": round(mean([t["hold_days_actual"] for t in trades]), 2) if trades else 0.0,
        "max_drawdown_r": round(max_dd, 3),
        "sharpe_estimate": round((mean(vals) / stdev(vals)) * math.sqrt(50), 2) if len(vals) >= 2 and stdev(vals) > 0 else 0.0,
        "expectancy": round(mean(vals), 3) if vals else 0.0,
    }

    by_regime = {k: substats([t for t in trades if t.get("regime") == k]) for k in sorted({t.get("regime", "NORMAL") for t in trades} or {"NORMAL"})}
    by_setup = {k: substats([t for t in trades if t.get("setup_type") == k]) for k in sorted({t.get("setup_type", "UNKNOWN") for t in trades} or {"UNKNOWN"})}

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "result_path": result_path,
        "params": params,
        "trades": trades,
        "equity_curve": curve,
        "stats": stats,
        "by_regime": by_regime,
        "by_setup_type": by_setup,
    }


def write_job(job_id: str | None, payload: dict[str, Any]) -> None:
    if not job_id:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / f"backtest_job_{job_id}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_universe(universe_size: int) -> list[str]:
    if not UNIVERSE_PATH.exists():
        raise FileNotFoundError(f"Universe file not found: {UNIVERSE_PATH}")
    raw = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    tickers = []
    for item in raw:
        ticker = item.get("ticker") if isinstance(item, dict) else item
        ticker = str(ticker or "").upper().strip()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    return tickers[:universe_size]


def run_backtest(
    start_date: str,
    end_date: str,
    universe_size: int = 100,
    setup_threshold: float = 55,
    rvol_gate: float = 2.0,
    hold_days: int = 5,
    stop_atr: float = 1.5,
    target_atr: float = 3.0,
    regime_filter: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    start_dt = datetime.fromisoformat(start_date).date()
    end_dt = datetime.fromisoformat(end_date).date()
    if end_dt <= start_dt:
        raise ValueError("end_date must be after start_date")
    if (end_dt - start_dt).days > 183 and universe_size > 100:
        raise ValueError("Range too large for FMP limits. Narrow to <=6 months or use <=100 stocks.")

    params = {
        "start_date": start_date,
        "end_date": end_date,
        "universe_size": int(universe_size),
        "setup_threshold": float(setup_threshold),
        "rvol_gate": float(rvol_gate),
        "hold_days": int(hold_days),
        "stop_atr": float(stop_atr),
        "target_atr": float(target_atr),
        "regime_filter": regime_filter or None,
    }
    tickers = load_universe(int(universe_size))
    fetch_start = (start_dt - timedelta(days=HISTORY_BUFFER_DAYS)).isoformat()
    run_cache: dict[str, list[dict]] = {}
    trades: list[dict] = []
    failures = 0

    write_job(job_id, {"status": "running", "progress": 0, "step": "Loading SPY", "trades_so_far": 0, "params": params})
    spy_bars = fetch_ohlcv("SPY", fetch_start, end_date, run_cache)
    spy_by_date = {b["date"]: b for b in spy_bars}

    for i, ticker in enumerate(tickers):
        pct = int((i / max(len(tickers), 1)) * 95)
        write_job(job_id, {"status": "running", "progress": pct, "step": f"Testing {ticker} ({i + 1}/{len(tickers)})", "trades_so_far": len(trades), "params": params})
        try:
            bars = fetch_ohlcv(ticker, fetch_start, end_date, run_cache)
            if len(bars) < 55:
                failures += 1
                if failures >= 5:
                    print("Circuit breaker: 5 consecutive data failures")
                    break
                continue
            failures = 0
            first_idx = next((j for j, b in enumerate(bars) if b["date"] >= start_date), 50)
            scan_start = max(50, first_idx)
            scan_end = max(scan_start, len(bars) - hold_days)
            for idx in range(scan_start, scan_end):
                signal = compute_signals(bars, spy_by_date, idx, setup_threshold, rvol_gate)
                if not signal:
                    continue
                regime = "NORMAL"
                if regime_filter and regime_filter.upper() not in {"ALL", regime}:
                    continue
                signal["ticker"] = ticker
                signal["regime"] = regime
                trade = simulate_trade(signal, bars[idx + 1: idx + 1 + hold_days], hold_days, stop_atr, target_atr)
                if trade:
                    trades.append(trade)
            if (i + 1) % 10 == 0:
                print(f"Progress: {i + 1}/{len(tickers)} tickers, {len(trades)} trades")
        except Exception as exc:
            print(f"Error processing {ticker}: {exc}")
            failures += 1
            if failures >= 5:
                print("Circuit breaker: 5 consecutive data failures")
                break

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result_file = DATA_DIR / f"backtest_results_{ts}.json"
    results = build_results(params, trades, str(result_file))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    write_job(job_id, {"status": "complete", "progress": 100, "step": f"Complete: {len(trades)} trades", "trades_so_far": len(trades), "params": params, "results": results})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="System 2 Backtest Engine")
    parser.add_argument("--start", required=False, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=False, help="End date YYYY-MM-DD")
    parser.add_argument("--period", default=None, help="Compatibility preset: last_1_month/last_3_months/last_6_months")
    parser.add_argument("--universe", type=int, default=100)
    parser.add_argument("--threshold", "--setup-threshold", dest="setup_threshold", type=float, default=55)
    parser.add_argument("--rvol-gate", "--min-rvol", dest="rvol_gate", type=float, default=2.0)
    parser.add_argument("--hold-days", type=int, default=5)
    parser.add_argument("--stop-atr", type=float, default=1.5)
    parser.add_argument("--target-atr", type=float, default=3.0)
    parser.add_argument("--regime-filter", default=None)
    parser.add_argument("--job-id", default=None)
    args = parser.parse_args()

    today = datetime.now(timezone.utc).date()
    if args.period and not args.start:
        days = {"last_1_month": 30, "last_3_months": 90, "last_6_months": 180}.get(args.period, 90)
        args.start = (today - timedelta(days=days)).isoformat()
        args.end = args.end or today.isoformat()
    args.start = args.start or (today - timedelta(days=90)).isoformat()
    args.end = args.end or today.isoformat()

    try:
        results = run_backtest(
            start_date=args.start,
            end_date=args.end,
            universe_size=args.universe,
            setup_threshold=args.setup_threshold,
            rvol_gate=args.rvol_gate,
            hold_days=args.hold_days,
            stop_atr=args.stop_atr,
            target_atr=args.target_atr,
            regime_filter=args.regime_filter,
            job_id=args.job_id,
        )
        print(json.dumps({"ok": True, "result_path": results["result_path"], "stats": results["stats"]}, indent=2))
    except Exception as exc:
        write_job(args.job_id, {"status": "error", "progress": 100, "error": str(exc)})
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
