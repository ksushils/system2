#!/usr/bin/env python3
"""
Intraday Entry-Trigger Monitor for System 2.

Market-hours polling loop that batch-fetches live prices for watchlist
finalists every 3 minutes, checks each against its entry conditions, fires
Telegram + dashboard alerts when conditions align, and keeps live prices
updated for finalist cards and Live Monitor cards.

ALERT-ONLY. No auto-execution. Human decides and enters manually.
Paper mode only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fmp_bandwidth
import requests
import threading

ROOT = Path(__file__).resolve().parent
FUND_DATA_PATH = Path("/root/fund-system/data/fund.json")
LIVE_PRICES_PATH = ROOT / "data" / "live_prices.json"
ALERT_LOG_PATH = ROOT / "data" / "entry_alerts.json"
OUTCOME_LOG_PATH = ROOT / "data" / "entry_alert_outcomes.json"
FINALIST_SOURCE_PATHS = [
    ROOT / "stage7_clustered_survivors.json",
    ROOT / "stage2_confluence_ranked_top40.json",
    ROOT / "stage2_surgical_strike_top40.json",
]

# Load .env from system2-core
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value

FMP_KEY = os.environ.get("FMP_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")

POLL_SECONDS = 180  # 3 minutes
MARKET_OPEN_UTC = 13.5  # 13:30 UTC = 09:30 ET (EDT)
INTRADAY_INTERVAL = "5min"          # FMP bars: 1min, 5min, 15min, 30min, 1hour
OR_BARS = 6                         # first 30 min = six 5-min bars
NEAR_ZONE_PCT = 0.03                # fetch bars only within 3% of entry zone
MAX_BOOTSTRAP_QUOTES_PER_CYCLE = 15 # cost ceiling for names with no cached live price
SPY_TICKER = "SPY"
MAX_BAR_CALLS_PER_CYCLE = 60        # hard cost ceiling
MARKET_CLOSE_UTC = 20.0  # 20:00 UTC = 16:00 ET (EDT)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_market_hours() -> bool:
    """Return True if current UTC time is within US equity market hours (Mon-Fri)."""
    now = now_utc()
    if now.weekday() >= 5:
        return False
    hour = now.hour + now.minute / 60.0
    return MARKET_OPEN_UTC <= hour <= MARKET_CLOSE_UTC


def _send_telegram_sync(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"TG error: {e}")


def send_telegram(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        print(f"ALERT (no TG): {msg}")
        return
    threading.Thread(target=_send_telegram_sync, args=(msg,), daemon=True).start()


def fmp_get(endpoint: str, params: dict[str, str] | None = None) -> Any:
    """Make a GET request to FMP and return parsed JSON."""
    query = urllib.parse.urlencode(params or {})
    sep = "&" if "?" in endpoint else "?"
    url = f"https://financialmodelingprep.com/{endpoint}{sep}apikey={urllib.parse.quote(FMP_KEY)}"
    if query:
        url += f"&{query}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "system2-entry-monitor/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        fmp_bandwidth.record(
            endpoint,
            len(raw),
            status=getattr(resp, "status", None),
            source="entry_trigger_monitor",
        )
        return json.loads(raw.decode("utf-8", "ignore"))


def get_watchlist() -> list[dict[str, Any]]:
    """Load watchlist finalists from fund.json — not entered and not closed."""
    if not FUND_DATA_PATH.exists():
        return []
    try:
        fund = json.loads(FUND_DATA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read fund.json: {exc}")
        return []

    ideas = fund.get("ideas", [])
    watch: list[dict[str, Any]] = []
    for i in ideas:
        if not isinstance(i, dict):
            continue
        entered = bool(
            i.get("actual_entry_price")
            or i.get("paper_entry_price")
            or i.get("entryRecorded")
            or i.get("paper_entry_date")
        )
        status = str(i.get("paper_status", "")).upper()
        if entered:
            continue
        if status in ("OPEN", "RESOLVED", "CLOSED", "EXPIRED", "INVALID"):
            continue

        entry_zone = i.get("entry_zone") or i.get("entryZone") or i.get("entry") or i.get("planned_entry")
        if not entry_zone:
            continue

        watch.append(i)
    return watch


def get_open_positions() -> list[dict[str, Any]]:
    """Load active paper positions that must always have fresh live prices."""
    if not FUND_DATA_PATH.exists():
        return []
    try:
        fund = json.loads(FUND_DATA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read fund.json open positions: {exc}")
        return []

    ideas = fund.get("ideas", [])
    return [
        i
        for i in ideas
        if isinstance(i, dict) and str(i.get("paper_status", "")).upper() == "OPEN"
    ]


def batch_quote(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """One FMP call for all watchlist tickers. Returns {ticker: quote}."""
    if not tickers or not FMP_KEY:
        return {}
    out: dict[str, dict[str, Any]] = {}
    unique_tickers = sorted({str(t).upper() for t in tickers if t})
    for i in range(0, len(unique_tickers), 50):
        chunk = unique_tickers[i : i + 50]
        syms = ",".join(chunk)
        try:
            data = fmp_get("stable/batch-quote", {"symbols": syms})
            if not isinstance(data, list):
                continue
            for q in data:
                sym = str(q.get("symbol", "")).upper()
                if not sym:
                    continue
                out[sym] = {
                    "price": q.get("price"),
                    "change_pct": q.get("changePercentage"),
                    "day_high": q.get("dayHigh"),
                    "day_low": q.get("dayLow"),
                    "volume": q.get("volume"),
                    "avg_volume": q.get("avgVolume"),
                    "timestamp": q.get("timestamp"),
                    "previous_close": q.get("previousClose"),
                    "open": q.get("open"),
                }
        except Exception as e:
            print(f"Batch quote error for chunk {chunk}: {e}")
    return out



def get_intraday_bars(ticker: str, interval: str = INTRADAY_INTERVAL) -> list[dict[str, Any]]:
    """Fetch today's intraday bars for a ticker. Returns oldest-first."""
    if not FMP_KEY:
        return []
    try:
        resp = requests.get(
            f"https://financialmodelingprep.com/stable/historical-chart/{interval}",
            params={"symbol": ticker, "apikey": FMP_KEY},
            timeout=12,
        )
        fmp_bandwidth.record(
            f"stable/historical-chart/{interval}",
            len(resp.content or b""),
            status=resp.status_code,
            source="entry_trigger_monitor",
        )
        if resp.status_code != 200:
            return []
        bars = resp.json()
        if not isinstance(bars, list):
            return []
        bars = list(reversed(bars))  # newest-first -> oldest-first
        today = now_utc().strftime("%Y-%m-%d")
        session = [b for b in bars if str(b.get("date", "")).startswith(today)]
        return [
            {
                "time": b.get("date"),
                "open": float(b.get("open", 0) or 0),
                "high": float(b.get("high", 0) or 0),
                "low": float(b.get("low", 0) or 0),
                "close": float(b.get("close", 0) or 0),
                "volume": float(b.get("volume", 0) or 0),
            }
            for b in session
        ]
    except Exception as e:
        print(f"Intraday bars {ticker}: {e}")
        return []


def compute_opening_range(bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Opening range = high/low of first 30 min."""
    if len(bars) < 2:
        return None
    or_bars = bars[:OR_BARS]
    or_high = max(b["high"] for b in or_bars)
    or_low = min(b["low"] for b in or_bars)
    current = bars[-1]["close"]
    broke_high = current > or_high
    broke_low = current < or_low
    in_range = or_low <= current <= or_high
    return {
        "or_high": round(or_high, 2),
        "or_low": round(or_low, 2),
        "current": round(current, 2),
        "broke_high": broke_high,
        "broke_low": broke_low,
        "in_range": in_range,
        "status": (
            "BREAKOUT" if broke_high else
            "BREAKDOWN" if broke_low else
            "IN_RANGE"
        ),
    }


def check_gap_held(bars: list[dict[str, Any]], premarket_status: str | None = None) -> dict[str, Any] | None:
    """Did a pre-market gap survive the open?"""
    if len(bars) < 3:
        return None
    session_open = bars[0]["open"]
    current = bars[-1]["close"]
    if session_open == 0:
        return None
    held_pct = (current - session_open) / session_open * 100
    return {
        "session_open": round(session_open, 2),
        "current": round(current, 2),
        "held_pct": round(held_pct, 2),
        "gap_held": held_pct > -0.5,
        "gap_extending": held_pct > 1.0,
        "premarket_status": premarket_status,
    }


def compute_intraday_vwap(bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """True intraday VWAP from session bars."""
    if not bars:
        return None
    cum_pv = 0.0
    cum_v = 0.0
    for b in bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        vol = b.get("volume", 0) or 0
        cum_pv += typical * vol
        cum_v += vol
    if cum_v == 0:
        return None
    vwap = cum_pv / cum_v
    current = bars[-1]["close"]
    pct_from_vwap = (current - vwap) / vwap * 100 if vwap else 0
    reclaimed = False
    if len(bars) >= 4:
        prev_close = bars[-3]["close"]
        if prev_close < vwap < current:
            reclaimed = True
    return {
        "vwap": round(vwap, 2),
        "current": round(current, 2),
        "pct_from_vwap": round(pct_from_vwap, 2),
        "above_vwap": current > vwap,
        "reclaimed_vwap": reclaimed,
    }


def compute_intraday_rs(ticker_bars: list[dict[str, Any]], spy_bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compare ticker's intraday % move to SPY's."""
    if not ticker_bars or not spy_bars:
        return None
    t_open = ticker_bars[0]["open"]
    t_now = ticker_bars[-1]["close"]
    s_open = spy_bars[0]["open"]
    s_now = spy_bars[-1]["close"]
    if not t_open or not s_open:
        return None
    t_move = (t_now - t_open) / t_open * 100
    s_move = (s_now - s_open) / s_open * 100
    rs = t_move - s_move
    return {
        "ticker_move_pct": round(t_move, 2),
        "spy_move_pct": round(s_move, 2),
        "intraday_rs": round(rs, 2),
        "outperforming": rs > 0.5,
    }


def get_session_phase() -> tuple[str, float, str]:
    """ET session phase and conviction weight."""
    now = now_utc()
    hour = now.hour + now.minute / 60
    et = hour - 4
    if 9.5 <= et < 10.5:
        return ("OPENING", 1.0, "High conviction — opening drive")
    elif 10.5 <= et < 11.5:
        return ("MORNING", 0.9, "Morning trend")
    elif 11.5 <= et < 14.0:
        return ("LUNCH", 0.6, "Lunch chop — lower conviction")
    elif 14.0 <= et < 15.0:
        return ("AFTERNOON", 0.8, "Afternoon setup")
    elif 15.0 <= et <= 16.0:
        return ("POWER_HOUR", 1.0, "Power hour — trend continuation")
    return ("OFF_HOURS", 0.0, "Outside session")


def _is_near_zone(price: float, ez_low: float | None, ez_high: float | None) -> bool:
    """Return True if price is within NEAR_ZONE_PCT of either zone boundary."""
    if ez_low is None or ez_high is None:
        return False
    low, high = min(ez_low, ez_high), max(ez_low, ez_high)
    if low <= price <= high:
        return True
    if price > 0 and abs(price - low) / price < NEAR_ZONE_PCT:
        return True
    if price > 0 and abs(price - high) / price < NEAR_ZONE_PCT:
        return True
    return False


def load_finalist_symbols() -> tuple[set[str], str | None]:
    for path in FINALIST_SOURCE_PATHS:
        if not path.exists():
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list) or not rows:
            continue
        symbols = {
            str(row.get("symbol") or row.get("ticker") or "").upper()
            for row in rows
            if isinstance(row, dict) and (row.get("symbol") or row.get("ticker"))
        }
        if symbols:
            return symbols, path.name
    return set(), None


def select_quote_watchlist(
    watch: list[dict[str, Any]],
    live_store: dict[str, Any],
    finalist_symbols: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Narrow live-price polling while always preserving open and pre-market favourable names."""
    finalist_symbols = finalist_symbols or set()
    finalist_gate_active = bool(finalist_symbols)
    selected: list[dict[str, Any]] = []
    skipped: list[str] = []
    bootstrap_count = 0
    reasons: list[dict[str, str]] = []

    for idea in watch:
        ticker = str(idea.get("ticker", "")).upper()
        if not ticker:
            continue
        status = str(idea.get("paper_status", "")).upper()
        if status == "OPEN":
            selected.append(idea)
            reasons.append({"ticker": ticker, "reason": "open_position"})
            continue
        pm = str(idea.get("pre_market_status") or idea.get("gap_status") or "").upper()
        pm_bool = idea.get("pre_market_gap_favourable") is True or idea.get("pre_market_favourable") is True
        if pm == "FAVOURABLE" or pm_bool:
            selected.append(idea)
            reasons.append({"ticker": ticker, "reason": "premarket_favourable"})
            continue
        if finalist_gate_active and ticker not in finalist_symbols:
            skipped.append(ticker)
            continue

        cached = live_store.get(ticker) if isinstance(live_store.get(ticker), dict) else {}
        cached_price = cached.get("last_price") if isinstance(cached, dict) else None
        try:
            price = float(cached_price)
        except Exception:
            price = 0.0
        ez_low, ez_high = parse_entry_zone(idea)
        if price > 0 and _is_near_zone(price, ez_low, ez_high):
            selected.append(idea)
            reasons.append({"ticker": ticker, "reason": "near_entry_zone"})
        elif price <= 0 and bootstrap_count < MAX_BOOTSTRAP_QUOTES_PER_CYCLE:
            selected.append(idea)
            bootstrap_count += 1
            reasons.append({"ticker": ticker, "reason": "bootstrap_missing_price"})
        else:
            skipped.append(ticker)

    reason_counts: dict[str, int] = {}
    for row in reasons:
        reason_counts[row["reason"]] = reason_counts.get(row["reason"], 0) + 1
    return selected, {
        "input_count": len(watch),
        "selected_count": len(selected),
        "unique_selected_ticker_count": len({str(i.get("ticker", "")).upper() for i in selected if i.get("ticker")}),
        "skipped_count": len(skipped),
        "skipped_tickers": skipped[:100],
        "selection_reasons": reasons[:200],
        "selection_reason_counts": reason_counts,
        "bootstrap_count": bootstrap_count,
        "finalist_gate_active": finalist_gate_active,
        "finalist_count": len(finalist_symbols),
        "premarket_favourable_selected": [
            str(i.get("ticker", "")).upper()
            for i in selected
            if (
                str(i.get("pre_market_status") or i.get("gap_status") or "").upper() == "FAVOURABLE"
                or i.get("pre_market_gap_favourable") is True
                or i.get("pre_market_favourable") is True
            )
        ],
        "open_position_selected": [
            str(i.get("ticker", "")).upper()
            for i in selected
            if str(i.get("paper_status", "")).upper() == "OPEN"
        ],
    }
def parse_entry_zone(idea: dict[str, Any]) -> tuple[float | None, float | None]:
    """Parse entry zone string or dict into (low, high)."""
    ez = idea.get("entry_zone") or idea.get("entryZone")
    if isinstance(ez, str) and "-" in ez:
        parts = ez.replace("$", "").split("-")
        try:
            return float(parts[0].strip()), float(parts[1].strip())
        except Exception:
            pass
    elif isinstance(ez, dict):
        low = ez.get("low") or ez.get("entry_low")
        high = ez.get("high") or ez.get("entry_high")
        try:
            return float(low), float(high)
        except Exception:
            pass
    # Try entry as a single number
    entry = idea.get("entry") or idea.get("planned_entry")
    try:
        e = float(str(entry).replace("$", ""))
        if e > 0:
            # Single-price entry: zone = +/- 0.5%
            return round(e * 0.995, 2), round(e * 1.005, 2)
    except Exception:
        pass
    return None, None


def check_entry_trigger(
    idea: dict[str, Any],
    quote: dict[str, Any],
    spy_bars: list[dict[str, Any]] | None = None,
    bar_calls: dict[str, int] | None = None,
) -> tuple[bool, list[str], str, dict[str, Any]]:
    """Check if this idea's entry conditions are met given current live price + intraday context."""
    price = quote.get("price")
    if price is None:
        return False, [], "NO_PRICE", {}
    try:
        price = float(price)
    except Exception:
        return False, [], "NO_PRICE", {}

    reasons: list[str] = []
    conditions_met = 0
    conditions_total = 0
    intraday: dict[str, Any] = {}

    # Condition 1 — price in entry zone
    ez_low, ez_high = parse_entry_zone(idea)
    in_zone = False
    conditions_total += 1
    if ez_low is not None and ez_high is not None:
        if ez_low <= price <= ez_high:
            conditions_met += 1
            in_zone = True
            reasons.append(f"In entry zone (${ez_low:.2f}-${ez_high:.2f})")
        elif price < ez_low:
            reasons.append(f"Below zone (${price:.2f} < ${ez_low:.2f})")
        else:
            reasons.append(f"Above zone (${price:.2f} > ${ez_high:.2f})")

    # Condition 2 — live RVOL confirming
    conditions_total += 1
    vol = quote.get("volume", 0) or 0
    avg_vol = quote.get("avg_volume", 0) or 0
    live_rvol = (vol / avg_vol) if avg_vol else 0.0
    if live_rvol >= 1.5:
        conditions_met += 1
        reasons.append(f"RVOL confirming ({live_rvol:.1f}x)")
    else:
        reasons.append(f"RVOL building ({live_rvol:.1f}x)")

    # Condition 3 — pre-market favourable
    conditions_total += 1
    pm = str(idea.get("pre_market_status") or idea.get("gap_status") or "").upper()
    if pm == "FAVOURABLE":
        conditions_met += 1
        reasons.append("Pre-market FAVOURABLE")

    # Condition 4 — not below stop already
    conditions_total += 1
    stop = idea.get("stop") or idea.get("stopLoss")
    if stop:
        try:
            stop_val = float(str(stop).replace("$", ""))
            if price > stop_val:
                conditions_met += 1
                reasons.append("Above stop")
            else:
                reasons.append(f"Below stop (${stop_val:.2f})")
        except Exception:
            pass

    # Intraday timing signals (only for tickers near zone, cost control)
    near_zone = _is_near_zone(price, ez_low, ez_high)
    bars_fetched = False
    if near_zone and bar_calls is not None and bar_calls.get("count", 0) < MAX_BAR_CALLS_PER_CYCLE:
        bars = get_intraday_bars(str(idea.get("ticker", "")).upper())
        bar_calls["count"] = bar_calls.get("count", 0) + 1
        bars_fetched = True
        if bars and spy_bars:
            intraday["opening_range"] = compute_opening_range(bars)
            intraday["gap"] = check_gap_held(bars, idea.get("pre_market_status") or idea.get("gap_status"))
            intraday["vwap"] = compute_intraday_vwap(bars)
            intraday["rs"] = compute_intraday_rs(bars, spy_bars)

            or_status = (intraday.get("opening_range") or {}).get("status")
            gap = intraday.get("gap") or {}
            vwap = intraday.get("vwap") or {}
            rs = intraday.get("rs") or {}

            if or_status == "BREAKOUT":
                conditions_met += 1
                reasons.append("✅ Broke opening range high")
            if gap.get("gap_held"):
                conditions_met += 1
                reasons.append("✅ Pre-market gap held")
            elif gap.get("gap_held") is False:
                reasons.append("⚠️ Gap faded — caution")
            if vwap.get("above_vwap"):
                conditions_met += 1
                reasons.append(f"✅ Above VWAP (${vwap['vwap']})")
            if vwap.get("reclaimed_vwap"):
                reasons.append("⭐ Just reclaimed VWAP")
            if rs.get("outperforming"):
                conditions_met += 1
                reasons.append(f"✅ Outperforming SPY (+{rs['intraday_rs']}%)")

    phase, weight, phase_msg = get_session_phase()
    intraday["_session_phase"] = {"phase": phase, "weight": weight, "note": phase_msg}
    intraday["_near_zone"] = near_zone
    intraday["_bars_fetched"] = bars_fetched
    idea["_intraday"] = intraday
    idea["_session_phase"] = phase

    # Trigger logic
    prime = (
        in_zone
        and pm == "FAVOURABLE"
        and (intraday.get("gap") or {}).get("gap_held")
        and (intraday.get("opening_range") or {}).get("status") == "BREAKOUT"
        and (intraday.get("vwap") or {}).get("above_vwap")
        and (intraday.get("rs") or {}).get("outperforming")
    )

    strength = "WAITING"
    if prime:
        strength = "PRIME_ENTRY"
    elif in_zone and pm == "FAVOURABLE":
        strength = "STRONG"
    elif in_zone and conditions_met >= 3:
        strength = "GOOD"
    elif in_zone and conditions_met >= 2:
        strength = "MODERATE"

    # Downgrade strength during lunch chop
    if weight < 0.7 and strength in ("STRONG", "PRIME_ENTRY"):
        strength = "GOOD"
        reasons.append(f"⏳ {phase_msg}")

    intraday["_is_prime"] = prime
    return in_zone and strength != "WAITING", reasons, strength, intraday


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_back_trigger_prices(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist future true-R fill evidence onto matching ideas without changing existing R fields."""
    if not alerts or not FUND_DATA_PATH.exists():
        return {"updated": 0, "alerts": len(alerts)}
    try:
        fund = json.loads(FUND_DATA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not write trigger prices: {exc}")
        return {"updated": 0, "alerts": len(alerts), "error": str(exc)}
    ideas = fund.get("ideas", [])
    updated = 0
    for alert in alerts:
        ticker = str(alert.get("ticker", "")).upper()
        price = alert.get("price_at_alert") or alert.get("price")
        if not ticker or price is None:
            continue
        for idea in ideas:
            if not isinstance(idea, dict):
                continue
            if str(idea.get("ticker", "")).upper() != ticker:
                continue
            status = str(idea.get("paper_status", "")).upper()
            if status in ("CLOSED", "RESOLVED", "INVALID"):
                continue
            if idea.get("trigger_price") is not None:
                continue
            idea["trigger_price"] = price
            idea["trigger_price_at"] = alert.get("alerted_at")
            idea["entry_alert_id"] = alert.get("id")
            idea["entry_alert_strength"] = alert.get("strength")
            idea["entry_trigger_source"] = "entry_trigger_monitor"
            updated += 1
            break
    if updated:
        FUND_DATA_PATH.write_text(json.dumps(fund, indent=2), encoding="utf-8")
    return {"updated": updated, "alerts": len(alerts)}


def run_cycle(force: bool = False) -> dict[str, Any]:
    if not force and not is_market_hours():
        return {"ok": True, "market_open": False, "checked": 0, "triggered": 0}

    watch = get_watchlist()
    open_positions = get_open_positions()
    quote_candidates = watch + open_positions
    if not quote_candidates:
        return {"ok": True, "market_open": True, "checked": 0, "triggered": 0, "message": "No watchlist or open ideas"}

    live_store = load_json(LIVE_PRICES_PATH, {})
    finalist_symbols, finalist_source = load_finalist_symbols()
    quote_watch, quote_selection = select_quote_watchlist(quote_candidates, live_store, finalist_symbols)
    quote_selection["finalist_source"] = finalist_source
    quote_selection["watchlist_count"] = len(watch)
    quote_selection["open_position_count"] = len(open_positions)
    quote_selection["unique_open_position_count"] = len({
        str(i.get("ticker", "")).upper()
        for i in open_positions
        if i.get("ticker")
    })
    tickers = [str(i.get("ticker")).upper() for i in quote_watch if i.get("ticker")]
    print(
        f"[{now_utc().strftime('%H:%M')}] Checking {len(tickers)}/{len(quote_candidates)} candidates "
        f"(skipped {quote_selection['skipped_count']} away from zone)"
    )

    quotes = batch_quote(tickers)

    # Fetch SPY intraday bars once per cycle for relative strength
    spy_bars = get_intraday_bars(SPY_TICKER)
    bar_calls: dict[str, int] = {"count": 0}

    alert_log = load_json(ALERT_LOG_PATH, {"alerts": []})
    outcome_log = load_json(OUTCOME_LOG_PATH, {"outcomes": []})

    today = now_utc().strftime("%Y-%m-%d")
    already_alerted = {
        a["ticker"]
        for a in alert_log.get("alerts", [])
        if a.get("date") == today
    }

    triggered: list[dict[str, Any]] = []
    session_alerted: set[str] = set()  # tickers alerted in this cycle

    for idea in quote_watch:
        ticker = str(idea.get("ticker", "")).upper()
        quote = quotes.get(ticker, {})
        price = quote.get("price")
        if price is None:
            continue

        ez_low, ez_high = parse_entry_zone(idea)

        # Always update live price store (dashboard reads this)
        live_store[ticker] = {
            "last_price": price,
            "change_pct": quote.get("change_pct"),
            "day_high": quote.get("day_high"),
            "day_low": quote.get("day_low"),
            "volume": quote.get("volume"),
            "avg_volume": quote.get("avg_volume"),
            "timestamp": quote.get("timestamp"),
            "updated_at": now_utc().isoformat(),
            "entry_zone": [ez_low, ez_high] if ez_low is not None else None,
            "entry_zone_low": ez_low,
            "entry_zone_high": ez_high,
            "stop": idea.get("stop"),
            "target": idea.get("target") or idea.get("tp1"),
        }

        if str(idea.get("paper_status", "")).upper() == "OPEN":
            continue

        # Only alert once per ticker per day across duplicate watchlist entries
        if ticker in already_alerted:
            continue

        fired, reasons, strength, intraday = check_entry_trigger(idea, quote, spy_bars, bar_calls)
        if fired and ticker not in already_alerted and ticker not in session_alerted:
            ez_low, ez_high = parse_entry_zone(idea)
            alert = {
                "id": f"{today}_{ticker}_{int(now_utc().timestamp())}",
                "ticker": ticker,
                "strength": strength,
                "price": price,
                "price_at_alert": price,
                "entry_zone": [ez_low, ez_high] if ez_low is not None else None,
                "entry_zone_low": ez_low,
                "entry_zone_high": ez_high,
                "stop": idea.get("stop"),
                "target": idea.get("target") or idea.get("tp1"),
                "reasons": reasons,
                "tier": idea.get("trade_readiness_tier", "?"),
                "date": today,
                "alerted_at": now_utc().isoformat(),
                "intraday": intraday,
                "session_phase": idea.get("_session_phase"),
            }
            triggered.append(alert)
            session_alerted.add(ticker)

    # Save live prices
    live_store["_updated_at"] = now_utc().isoformat()
    save_json(LIVE_PRICES_PATH, live_store)

    # Fire alerts
    for t in triggered:
        emoji = "⭐" if t["strength"] == "STRONG" else "🔔"
        msg = (
            f"{emoji} <b>ENTRY TRIGGER — {t['ticker']}</b> ({t['strength']})\n"
            f"Tier: {t['tier']} | Price: ${float(t['price']):.2f}\n"
            f"Entry zone: {t['entry_zone']}\n\n"
            + "\n".join(t["reasons"])
            + "\n\n<i>Conditions aligned. Review and enter manually if you agree.</i>"
        )
        send_telegram(msg)
        print(f"ALERT: {t['ticker']} ({t['strength']})")

    trigger_writeback = write_back_trigger_prices(triggered)

    # Log alerts
    alerts = triggered + alert_log.get("alerts", [])
    alert_log["alerts"] = alerts[:200]
    alert_log["last_cycle"] = now_utc().isoformat()
    by_strength: dict[str, int] = {"PRIME_ENTRY": 0, "STRONG": 0, "GOOD": 0, "MODERATE": 0}
    for a in alerts:
        if a.get("date") == today and a.get("strength") in by_strength:
            by_strength[a["strength"]] += 1
    alert_log["summary"] = {
        "total_alerts": len(alerts),
        "today_alerts": len([a for a in alerts if a.get("date") == today]),
        "new_this_cycle": len(triggered),
        "by_strength": by_strength,
        "bar_calls_this_cycle": bar_calls.get("count", 0),
        "quote_selection": quote_selection,
        "spy_bars_available": bool(spy_bars),
        "last_triggered": [t["ticker"] for t in triggered],
        "trigger_price_writeback": trigger_writeback,
    }
    save_json(ALERT_LOG_PATH, alert_log)

    # Seed outcome records for new alerts
    existing_ids = {o.get("alert_id") for o in outcome_log.get("outcomes", [])}
    for t in triggered:
        if t["id"] not in existing_ids:
            outcome_log["outcomes"].append({
                "alert_id": t["id"],
                "ticker": t["ticker"],
                "date": t["date"],
                "price_at_alert": t["price_at_alert"],
                "strength": t["strength"],
                "session_phase": t.get("session_phase"),
                "intraday_signals": {
                    "opening_range_breakout": (t.get("intraday") or {}).get("opening_range", {}).get("status") == "BREAKOUT",
                    "gap_held": (t.get("intraday") or {}).get("gap", {}).get("gap_held", False),
                    "above_vwap": (t.get("intraday") or {}).get("vwap", {}).get("above_vwap", False),
                    "reclaimed_vwap": (t.get("intraday") or {}).get("vwap", {}).get("reclaimed_vwap", False),
                    "outperforming_spy": (t.get("intraday") or {}).get("rs", {}).get("outperforming", False),
                },
                "price_2d_later": None,
                "price_5d_later": None,
                "move_2d_pct": None,
                "move_5d_pct": None,
            })
    save_json(OUTCOME_LOG_PATH, outcome_log)

    print(f"Cycle done. {len(triggered)} new alerts. Bar calls: {bar_calls.get('count', 0)}")
    return {
        "ok": True,
        "market_open": True,
        "checked": len(quote_watch),
        "watchlist_count": len(watch),
        "open_position_count": len(open_positions),
        "quote_selection": quote_selection,
        "triggered": len(triggered),
        "triggered_tickers": [t["ticker"] for t in triggered],
        "trigger_price_writeback": trigger_writeback,
        "missing_prices": [t for t in tickers if t not in quotes],
        "bar_calls": bar_calls.get("count", 0),
        "spy_bars_available": bool(spy_bars),
    }


def update_outcomes() -> dict[str, Any]:
    """Nightly helper: fill price_2d_later and price_5d_later for past alerts."""
    outcome_log = load_json(OUTCOME_LOG_PATH, {"outcomes": []})
    alerts = load_json(ALERT_LOG_PATH, {"alerts": []}).get("alerts", [])

    # Collect tickers and dates needing price lookups
    need: dict[str, set[str]] = {}
    for o in outcome_log.get("outcomes", []):
        if o.get("price_2d_later") is not None and o.get("price_5d_later") is not None:
            continue
        ticker = o.get("ticker")
        date = o.get("date")
        if not ticker or not date:
            continue
        need.setdefault(ticker, set()).add(date)

    if not need:
        return {"updated": 0}

    # Fetch historical prices for all tickers
    history: dict[str, dict[str, float]] = {}
    for ticker, dates in need.items():
        try:
            data = fmp_get(f"stable/historical-price-eod/{ticker}", {"limit": "30"})
            if not isinstance(data, list):
                continue
            history[ticker] = {}
            for row in data:
                day = row.get("date")
                close = row.get("close")
                if day and close is not None:
                    history[ticker][day] = float(close)
        except Exception as e:
            print(f"History fetch error {ticker}: {e}")

    updated = 0
    for o in outcome_log.get("outcomes", []):
        ticker = o.get("ticker")
        date = o.get("date")
        hist = history.get(ticker, {})
        if not hist or not date:
            continue
        # Find 2 trading days and 5 trading days after alert date
        sorted_dates = sorted(hist.keys())
        try:
            idx = sorted_dates.index(date)
        except ValueError:
            continue
        if o.get("price_2d_later") is None and idx + 2 < len(sorted_dates):
            d2 = sorted_dates[idx + 2]
            o["price_2d_later"] = hist[d2]
            o["move_2d_pct"] = round((hist[d2] / o["price_at_alert"] - 1) * 100, 2) if o["price_at_alert"] else None
            updated += 1
        if o.get("price_5d_later") is None and idx + 5 < len(sorted_dates):
            d5 = sorted_dates[idx + 5]
            o["price_5d_later"] = hist[d5]
            o["move_5d_pct"] = round((hist[d5] / o["price_at_alert"] - 1) * 100, 2) if o["price_at_alert"] else None
            updated += 1

    save_json(OUTCOME_LOG_PATH, outcome_log)
    return {"updated": updated}


def outcome_summary() -> dict[str, Any]:
    """Return accuracy summary for entry alerts."""
    outcome_log = load_json(OUTCOME_LOG_PATH, {"outcomes": []})
    outcomes = outcome_log.get("outcomes", [])
    by_strength: dict[str, list[dict[str, Any]]] = {}
    for o in outcomes:
        s = o.get("strength", "UNKNOWN")
        by_strength.setdefault(s, []).append(o)

    summary = {"total_alerts": len(outcomes), "by_strength": {}, "by_intraday_signal": {}}
    for strength, items in by_strength.items():
        moves_2d = [o["move_2d_pct"] for o in items if o.get("move_2d_pct") is not None]
        moves_5d = [o["move_5d_pct"] for o in items if o.get("move_5d_pct") is not None]
        summary["by_strength"][strength] = {
            "count": len(items),
            "avg_2d_move_pct": round(sum(moves_2d) / len(moves_2d), 2) if moves_2d else None,
            "avg_5d_move_pct": round(sum(moves_5d) / len(moves_5d), 2) if moves_5d else None,
        }

    signal_keys = ["opening_range_breakout", "gap_held", "above_vwap", "reclaimed_vwap", "outperforming_spy"]
    for key in signal_keys:
        sig_outcomes = [o for o in outcomes if (o.get("intraday_signals") or {}).get(key)]
        moves_2d = [o["move_2d_pct"] for o in sig_outcomes if o.get("move_2d_pct") is not None]
        moves_5d = [o["move_5d_pct"] for o in sig_outcomes if o.get("move_5d_pct") is not None]
        summary["by_intraday_signal"][key] = {
            "count": len(sig_outcomes),
            "avg_2d_move_pct": round(sum(moves_2d) / len(moves_2d), 2) if moves_2d else None,
            "avg_5d_move_pct": round(sum(moves_5d) / len(moves_5d), 2) if moves_5d else None,
        }
    return summary


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--update-outcomes":
        result = update_outcomes()
        print(json.dumps(result, indent=2))
        return
    if args and args[0] == "--outcome-summary":
        print(json.dumps(outcome_summary(), indent=2))
        return
    if args and args[0] == "--run-once":
        result = run_cycle(force="--force" in args)
        print(json.dumps(result, indent=2))
        return

    print("Entry-Trigger Monitor starting...")
    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"Cycle error: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
