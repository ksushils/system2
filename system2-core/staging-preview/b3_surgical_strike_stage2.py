#!/usr/bin/env python3
"""
B3 Surgical Strike Stage 2 technical scorer.

Inputs:
  - stage1_survivors.json from B2

Outputs:
  - stage1_survivors_no_strc.json
  - stage2_surgical_strike_scored.json
  - stage2_surgical_strike_top40.json
  - stage2_surgical_strike_enriched.json
  - stage2_enrichment_rejections.json
  - stage2_surgical_strike_metadata.json

This runner keeps the Surgical Strike technical shape only:
VWAP, relative volume, ATR, RS vs SPY, sector/ETF gate, and momentum.
It does not place orders, call brokers, call Chronos, call AI models, or deploy.

Stage 2 enrichment integrations (applied after top-40 selection, before output):
  - Earnings Date Hard Gate (FMP stable/earnings)
  - Danelfin Scores
  - FMP Insider Buying
  - FMP Shares Float + StockAnalysis Short Interest fallback
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path.home() / "Downloads"
STAGE1_PATH = ROOT / "stage1_survivors.json"
STAGE1_NO_STRC_PATH = ROOT / "stage1_survivors_no_strc.json"
SCORED_PATH = ROOT / "stage2_surgical_strike_scored.json"
TOP40_PATH = ROOT / "stage2_surgical_strike_top40.json"
ENRICHED_ALL_PATH = ROOT / "stage2_surgical_strike_enriched.json"
REJECTIONS_PATH = ROOT / "stage2_enrichment_rejections.json"
META_PATH = ROOT / "stage2_surgical_strike_metadata.json"
DANELFIN_PATH = Path("/root/system2-core/data/danelfin_scores.json")
STOCKANALYSIS_PATH = Path("/root/system2-core/data/stockanalysis_scores.json")

FMP_BASE = "https://financialmodelingprep.com"


def load_config() -> dict:
    path = ROOT / "system2-config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


CONFIG = load_config()
STAGE2_CONFIG = CONFIG.get("stage2", {})
TOP_N = int(STAGE2_CONFIG.get("top_n", 40))
MAX_WORKERS = int(STAGE2_CONFIG.get("max_workers", 4))
ACCOUNT_SIZE = float(CONFIG.get("stage7", {}).get("account_size", 25_000))
RISK_PCT = float(CONFIG.get("stage7", {}).get("risk_pct", 0.01))
RISK_DOLLARS = round(ACCOUNT_SIZE * RISK_PCT, 2)
MAX_STAGE2_SHARES = int(STAGE2_CONFIG.get("max_stage2_shares", 1000))
DAILY_ATR_LOOKBACK = int(STAGE2_CONFIG.get("daily_atr_lookback", 20))
DAILY_OHLCV_LOOKBACK = 60

SECTOR_ETFS = {
    "Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
}

BLOCKED_TICKERS = {
    "STRC",  # MicroStrategy preferred stock, not common equity.
}


def load_fmp_key() -> str:
    env_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if env_key:
        return env_key.strip()

    for path in [
        DOWNLOADS / "FMP-Scanner-v13.5-alpaca.json",
        DOWNLOADS / "FMP_Scanner_FIXED.json",
        DOWNLOADS / "universe_builder.py",
    ]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"FMP_API_KEY:\s*'([^']+)'", text)
        if match:
            return match.group(1)
        match = re.search(r"FMP_API_KEY\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            return match.group(1)
    raise RuntimeError("FMP API key not found. Set FMP_API_KEY.")


class FmpClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.calls = 0
        self.errors: list[str] = []

    def get(self, endpoint: str, timeout: int = 30):
        sep = "&" if "?" in endpoint else "?"
        url = f"{FMP_BASE}/{endpoint}{sep}apikey={urllib.parse.quote(self.api_key)}"
        self.calls += 1
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "system2-b3-stage2/1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", "ignore")
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: HTTP {exc.code}")
                return None
            except Exception as exc:
                if attempt < 3:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: {exc}")
                return None
        return None


def num(value, default=0.0) -> float:
    try:
        value = float(value)
        if math.isfinite(value):
            return value
    except Exception:
        pass
    return default


def pct(a: float, b: float) -> float:
    return ((a - b) / b) * 100.0 if b else 0.0


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


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains += max(0.0, diff)
        losses += max(0.0, -diff)
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(0.0, diff)) / period
        avg_loss = (avg_loss * (period - 1) + max(0.0, -diff)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        return 0.0
    cur = sum(trs[:period]) / period
    for tr in trs[period:]:
        cur = ((cur * (period - 1)) + tr) / period
    return cur


def daily_atr14(daily_raw: list[dict]) -> float | None:
    if not isinstance(daily_raw, list) or len(daily_raw) < 15:
        return None
    bars = sorted(daily_raw, key=lambda row: str(row.get("date", "")))
    trs: list[float] = []
    for i in range(max(1, len(bars) - 14), len(bars)):
        high = num(bars[i].get("high"))
        low = num(bars[i].get("low"))
        prev_close = num(bars[i - 1].get("close"))
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / len(trs) if len(trs) == 14 else None


def parse_daily_history(data) -> list[dict]:
    if isinstance(data, dict):
        hist = data.get("historical")
        if isinstance(hist, list):
            return hist
        for key in ("data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return data if isinstance(data, list) else []


def fetch_daily_history(client: FmpClient, symbol: str) -> list[dict]:
    # FMP's legacy /historical-price-full/{symbol} shape now 404s on this account;
    # the stable EOD endpoint is the current equivalent daily OHLCV source.
    endpoint = (
        "stable/historical-price-eod/full"
        f"?symbol={urllib.parse.quote(symbol)}&timeseries={DAILY_OHLCV_LOOKBACK}"
    )
    data = client.get(endpoint)
    rows = parse_daily_history(data)
    if rows:
        return rows[:DAILY_OHLCV_LOOKBACK]
    return []


def normalize_daily_ohlcv(daily_bars: list[dict]) -> list[dict]:
    normalized = []
    for row in daily_bars[:DAILY_OHLCV_LOOKBACK]:
        date = str(row.get("date") or "")[:10]
        if not date:
            continue
        normalized.append({
            "date": date,
            "open": num(row.get("open")),
            "high": num(row.get("high")),
            "low": num(row.get("low")),
            "close": num(row.get("close")),
            "volume": num(row.get("volume")),
        })
    return sorted(normalized, key=lambda row: row["date"])


def apply_daily_atr_risk(setup: dict, daily_bars: list[dict]) -> dict:
    symbol = setup.get("symbol")
    # FMP returns newest-first. Keep ATR behavior scoped to the newest 20 bars.
    atr_bars = daily_bars[:DAILY_ATR_LOOKBACK]
    daily_atr = daily_atr14(atr_bars)
    ohlcv_60 = normalize_daily_ohlcv(daily_bars)
    if not daily_atr or daily_atr <= 0:
        return {
            **setup,
            "status": "REJECTED",
            "stage2RejectReason": "missing_daily_atr14",
            "rejectReasons": list(setup.get("rejectReasons") or []) + ["missing_daily_atr14"],
            "ohlcv_60": ohlcv_60,
        }
    entry = num(setup.get("price"))
    ordered_daily = sorted(atr_bars, key=lambda row: str(row.get("date", "")))
    closes_20 = [num(row.get("close")) for row in ordered_daily[-20:] if num(row.get("close")) > 0]
    ma20 = sum(closes_20) / len(closes_20) if closes_20 else None
    volumes_20 = [num(row.get("volume")) for row in ordered_daily[-20:] if num(row.get("volume")) >= 0]
    avg_volume_20d = sum(volumes_20) / len(volumes_20) if volumes_20 else None
    current_daily_volume = num(ordered_daily[-1].get("volume")) if ordered_daily else None
    daily_rvol_20d = (
        current_daily_volume / avg_volume_20d
        if current_daily_volume is not None and avg_volume_20d
        else None
    )
    price_above_20d_ma = bool(ma20 is not None and entry > ma20)
    rvol_healthy = bool(daily_rvol_20d is not None and daily_rvol_20d >= 0.8)
    risk_per_share = daily_atr * 1.5
    stop = entry - risk_per_share
    tp1 = entry + risk_per_share * 2.0
    tp2 = entry + risk_per_share * 3.0
    stop_warning = "VERY WIDE STOP" if stop < entry * 0.90 else None
    raw_shares = math.floor(RISK_DOLLARS / risk_per_share) if risk_per_share > 0 else 0
    if raw_shares < 1:
        return {
            **setup,
            "status": "REJECTED",
            "stage2RejectReason": "position_too_small",
            "rejectReasons": list(setup.get("rejectReasons") or []) + ["position_too_small"],
            "atr_daily": round(daily_atr, 4),
            "atr14": round(daily_atr, 4),
            "riskPerShare": round(risk_per_share, 4),
            "ohlcv_60": ohlcv_60,
        }
    position_shares = min(raw_shares, MAX_STAGE2_SHARES)
    position_risk_dollars = position_shares * risk_per_share
    reasons = list(setup.get("scoreReasons") or [])
    if stop_warning and stop_warning not in reasons:
        reasons.append(stop_warning)
    return {
        **setup,
        "ma20": round(ma20, 4) if ma20 is not None else None,
        "above_20d_trend": price_above_20d_ma,
        "price_above_20d_ma": price_above_20d_ma,
        "avg_volume_20d": round(avg_volume_20d, 2) if avg_volume_20d is not None else None,
        "current_daily_volume": current_daily_volume,
        "daily_rvol_20d": round(daily_rvol_20d, 4) if daily_rvol_20d is not None else None,
        "rvol_healthy": rvol_healthy,
        "track_a_fail": not (price_above_20d_ma and rvol_healthy),
        "atr_daily": round(daily_atr, 4),
        "atr14": round(daily_atr, 4),
        "atrPct": round((daily_atr / entry) * 100, 3) if entry else None,
        "riskSource": "daily_atr14",
        "stopLoss": round(stop, 4),
        "riskPerShare": round(risk_per_share, 4),
        "stopAtrMultiple": 1.5,
        "stopWarning": stop_warning,
        "tp1": round(tp1, 4),
        "tp2": round(tp2, 4),
        "positionShares": position_shares,
        "positionRiskDollars": round(position_risk_dollars, 2),
        "positionSizingRule": "floor(250 / (daily ATR14 * 1.5)), cap 1000 shares",
        "entryZone": [round(entry * 0.995, 4), round(entry * 1.005, 4)],
        "scoreReasons": reasons[:10],
        "ohlcv_60": ohlcv_60,
    }


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) <= period * 2:
        return 20.0
    tr_arr: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(closes)):
        tr_arr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    atr_sum = sum(tr_arr[:period])
    plus_sum = sum(plus_dm[:period])
    minus_sum = sum(minus_dm[:period])
    dxs: list[float] = []
    for i in range(period, len(tr_arr)):
        atr_sum = atr_sum - atr_sum / period + tr_arr[i]
        plus_sum = plus_sum - plus_sum / period + plus_dm[i]
        minus_sum = minus_sum - minus_sum / period + minus_dm[i]
        pdi = 100 * plus_sum / atr_sum if atr_sum else 0
        mdi = 100 * minus_sum / atr_sum if atr_sum else 0
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    if len(dxs) < period:
        return 20.0
    cur = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        cur = ((cur * (period - 1)) + dx) / period
    return cur


def vwap(candles: list[dict]) -> float:
    tpv = vol = 0.0
    for c in candles:
        h, l, close, v = num(c.get("high")), num(c.get("low")), num(c.get("close")), num(c.get("volume"))
        typical = (h + l + close) / 3.0
        tpv += typical * v
        vol += v
    return tpv / vol if vol else num(candles[-1].get("close")) if candles else 0.0


def get_today(candles: list[dict]) -> list[dict]:
    if not candles:
        return []
    latest_date = str(candles[-1].get("date", ""))[:10]
    return [c for c in candles if str(c.get("date", "")).startswith(latest_date)]


def load_danelfin_scores(path: Path = DANELFIN_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("scores", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_stockanalysis_scores(path: Path = STOCKANALYSIS_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def enrich_earnings(ticker: str, fmp_key: str) -> dict:
    """Fetch upcoming earnings date from FMP and classify risk. Fail-open."""
    client = FmpClient(fmp_key)
    today = datetime.now(timezone.utc).date()
    earnings_date: str | None = None
    try:
        data = client.get(f"stable/earnings?symbol={urllib.parse.quote(ticker)}", timeout=10)
        rows = data if isinstance(data, list) else []
        dates: list[date] = []
        for r in rows:
            d = r.get("date") or r.get("earningsDate")
            if not d:
                continue
            try:
                dt = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                dates.append(dt)
            except Exception:
                continue
        future_dates = sorted([d for d in dates if d >= today])
        if future_dates:
            earnings_date = future_dates[0].isoformat()
    except Exception:
        pass

    days_to_earnings: int | None = None
    earnings_in_window = False
    earnings_risk = "UNKNOWN"
    note: str | None = None
    if earnings_date:
        ed = datetime.strptime(earnings_date, "%Y-%m-%d").date()
        days = (ed - today).days
        days_to_earnings = int(days)
        earnings_in_window = days_to_earnings <= 14
        if days > 14:
            earnings_risk = "CLEAR"
        elif 8 <= days <= 14:
            earnings_risk = "CAUTION"
            note = "Earnings approaching"
        elif 1 <= days <= 7:
            earnings_risk = "DANGER"
        else:  # days <= 0
            earnings_risk = "DANGER"

    return {
        "earnings_date": earnings_date,
        "days_to_earnings": days_to_earnings,
        "earnings_in_window": earnings_in_window,
        "earnings_risk": earnings_risk,
        "earnings_note": note,
        "earnings_data_available": bool(earnings_date),
    }


def enrich_danelfin(row: dict, danelfin_scores: dict) -> dict:
    """Enrich a row with Danelfin scores. Does not reject here; gate applied downstream."""
    ticker = row.get("symbol", "")
    scores = danelfin_scores.get(ticker) or {}
    if not isinstance(scores, dict):
        scores = {}
    data_available = bool(scores) and scores.get("ai_score") is not None
    return {
        "danelfin_ai_score": scores.get("ai_score"),
        "danelfin_technical": scores.get("technical"),
        "danelfin_fundamental": scores.get("fundamental"),
        "danelfin_sentiment": scores.get("sentiment"),
        "danelfin_low_risk": scores.get("low_risk"),
        "danelfin_buy_track_record": scores.get("buy_track_record"),
        "danelfin_sell_track_record": scores.get("sell_track_record"),
        "danelfin_sector": scores.get("sector"),
        "danelfin_industry": scores.get("industry"),
        "danelfin_score_trend": scores.get("score_trend"),
        "danelfin_score_upgraded_5d": scores.get("score_upgraded_5d"),
        "danelfin_score_change_5d": scores.get("score_change_5d"),
        "danelfin_data_available": data_available,
    }


def enrich_insider_trading(ticker: str, fmp_key: str) -> dict:
    """Fetch last 30 days of FMP insider purchases. Fail-open."""
    client = FmpClient(fmp_key)
    buys: list[dict] = []
    data_available = False
    try:
        data = client.get(f"stable/insider-trading?symbol={urllib.parse.quote(ticker)}", timeout=10)
        rows = data if isinstance(data, list) else []
        data_available = data is not None
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=30)
        for r in rows:
            tx_type = str(r.get("transactionType") or "").strip().upper()
            if tx_type != "P-PURCHASE":
                continue
            d = r.get("transactionDate") or r.get("filingDate") or r.get("date")
            if not d:
                continue
            try:
                dt = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if dt < cutoff:
                continue
            value = num(r.get("transactionPrice"), 0.0) * num(r.get("securitiesTransacted"), 0.0)
            if value <= 0:
                value = num(r.get("value"), 0.0)
            buyer = r.get("reportingName") or r.get("fullName") or r.get("insiderName")
            buys.append({"date": dt.isoformat(), "value": value, "buyer": buyer})
    except Exception:
        pass

    insider_buys_30d = len(buys)
    insider_buy_value_30d = round(sum(b["value"] for b in buys), 2)
    max_buy = max((b["value"] for b in buys), default=0.0)
    latest = max(buys, key=lambda b: b["date"]) if buys else None

    if insider_buys_30d >= 2 or max_buy >= 500_000:
        signal = "STRONG"
    elif insider_buys_30d == 1:
        signal = "MODERATE"
    else:
        signal = "NONE"

    return {
        "insider_buys_30d": insider_buys_30d,
        "insider_buy_value_30d": insider_buy_value_30d,
        "insider_buy_signal": signal,
        "insider_latest_buyer": latest["buyer"] if latest else None,
        "insider_latest_buy_date": latest["date"] if latest else None,
        "insider_data_available": data_available,
    }


def enrich_short_interest(ticker: str, fmp_key: str, row: dict, stockanalysis_scores: dict | None = None) -> dict:
    """Fetch short interest from FMP and fallback to StockAnalysis. Fail-open."""
    client = FmpClient(fmp_key)
    short_interest: float | None = None
    short_percent_float_dec: float | None = None
    short_ratio_days_to_cover: float | None = None
    float_shares: float | None = None
    fmp_data_available = False

    try:
        vol_data = client.get(f"stable/short-volume?symbol={urllib.parse.quote(ticker)}", timeout=10)
        float_data = client.get(f"stable/shares-float?symbol={urllib.parse.quote(ticker)}", timeout=10)

        if isinstance(vol_data, list) and vol_data:
            latest = vol_data[0]
            short_interest = num(latest.get("shortVolume") or latest.get("shortInterest"), None) or None
            short_percent_float_dec = num(latest.get("shortPercent") or latest.get("shortPercentFloat"), None) or None
            fmp_data_available = True

        if isinstance(float_data, list) and float_data:
            latest_float = float_data[0]
            float_shares = num(latest_float.get("floatShares") or latest_float.get("float"), None) or None
            fmp_data_available = True
            if short_interest is None and short_percent_float_dec is None and float_shares:
                short_percent_float_dec = num(latest_float.get("shortPercentFloat"), None) or None
    except Exception:
        pass

    # StockAnalysis fallback
    sa = (stockanalysis_scores or load_stockanalysis_scores()).get(ticker) or {}
    if sa.get("short_percent_float") is not None and short_percent_float_dec is None:
        short_percent_float_dec = num(sa["short_percent_float"], None)
    if sa.get("float_shares") is not None and float_shares is None:
        float_shares = num(sa["float_shares"], None)
    if short_interest is None and short_percent_float_dec is not None and float_shares:
        short_interest = round(short_percent_float_dec * float_shares, 0)
    if short_ratio_days_to_cover is None and short_interest is not None and sa.get("average_volume_30d"):
        short_ratio_days_to_cover = short_interest / num(sa["average_volume_30d"], 0.0)

    # Short-squeeze score: +1 each for short_pct_float > 15%, DTC > 5, high RVOL, above 20d MA
    squeeze_score = 0
    spf_dec = num(short_percent_float_dec, 0.0)
    dtc = num(short_ratio_days_to_cover, 0.0)
    if spf_dec > 0.15:
        squeeze_score += 1
    if dtc > 5:
        squeeze_score += 1
    if row.get("rvol_healthy") is True:
        squeeze_score += 1
    if row.get("above_20d_trend") is True:
        squeeze_score += 1

    if squeeze_score >= 3:
        short_signal = "SQUEEZE_CANDIDATE"
    elif spf_dec > 0.20:
        short_signal = "HIGH_SHORT"
    elif spf_dec >= 0.10:
        short_signal = "MODERATE"
    elif spf_dec > 0:
        short_signal = "LOW"
    else:
        short_signal = "UNKNOWN"

    return {
        "short_interest": int(short_interest) if short_interest is not None else None,
        "short_percent_float": round(spf_dec * 100, 4) if short_percent_float_dec is not None else None,
        "short_ratio_days_to_cover": round(dtc, 4) if dtc else None,
        "float_shares": int(float_shares) if float_shares is not None else None,
        "short_squeeze_score": squeeze_score,
        "short_signal": short_signal,
        "short_data_available": fmp_data_available or bool(sa),
    }


def apply_stage2_enrichments_and_gates(rows: list[dict], fmp_key: str) -> tuple[list[dict], list[dict]]:
    """Run Stage 2 enrichment integrations after top-40 selection. Returns (pass, reject)."""
    danelfin_scores = load_danelfin_scores()
    stockanalysis_scores = load_stockanalysis_scores()
    enriched_rows: list[dict] = []
    rejected_rows: list[dict] = []
    earnings_rejects = 0
    danelfin_rejects = 0

    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        earnings = enrich_earnings(symbol, fmp_key)
        danelfin = enrich_danelfin(row, danelfin_scores)
        insider = enrich_insider_trading(symbol, fmp_key)
        short = enrich_short_interest(symbol, fmp_key, row, stockanalysis_scores)
        # Throttle FMP calls to avoid rate-limiting on 40+ symbols
        if len(rows) >= 40:
            time.sleep(0.25)
        merged = {**row, **earnings, **danelfin, **insider, **short}
        rejected = False

        if earnings.get("earnings_risk") == "DANGER":
            days = earnings.get("days_to_earnings")
            if days is not None and days > 0:
                reason = f"earnings_too_close_{days}d"
            else:
                reason = "earnings_today_or_past"
            merged["status"] = "REJECTED"
            merged["stage2RejectReason"] = reason
            merged["rejectReasons"] = list(row.get("rejectReasons") or []) + [reason]
            rejected_rows.append(merged)
            earnings_rejects += 1
            rejected = True
        elif danelfin.get("danelfin_data_available") and danelfin.get("danelfin_ai_score") is not None and danelfin["danelfin_ai_score"] < 5:
            reason = "danelfin_ai_score_low"
            merged["status"] = "REJECTED"
            merged["stage2RejectReason"] = reason
            merged["rejectReasons"] = list(row.get("rejectReasons") or []) + [reason]
            rejected_rows.append(merged)
            danelfin_rejects += 1
            rejected = True

        if not rejected:
            enriched_rows.append(merged)

    print(f"Earnings rejects: {earnings_rejects}")
    print(f"Danelfin rejects: {danelfin_rejects}")
    print(f"Enriched: {len(enriched_rows)}")
    return enriched_rows, rejected_rows


def score_symbol(item: dict, candles_raw: list[dict], spy_today_ret: float, sector_ret: float | None) -> dict:
    symbol = item["symbol"]
    if not isinstance(candles_raw, list) or len(candles_raw) < 30:
        return {**item, "status": "REJECTED", "stage2RejectReason": "insufficient_5min_candles", "setupQualityScore": 0}

    candles = list(reversed(candles_raw))
    closes = [num(c.get("close")) for c in candles]
    highs = [num(c.get("high")) for c in candles]
    lows = [num(c.get("low")) for c in candles]
    volumes = [num(c.get("volume")) for c in candles]

    today = get_today(candles)
    vwap_candles = today if len(today) >= 5 else candles[-78:]
    cur_close = closes[-1]
    cur_vwap = vwap(vwap_candles)
    distance_from_vwap = pct(cur_close, cur_vwap)

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)
    cur_ema9 = ema9[-1] or cur_close
    cur_ema21 = ema21[-1] or cur_close
    cur_ema50 = ema50[-1] or cur_ema21
    bull_stack = cur_ema9 > cur_ema21 > cur_ema50

    cur_atr = atr(highs, lows, closes, 14)
    cur_rsi = rsi(closes, 14)
    cur_adx = adx(highs, lows, closes, 14)

    vol_window = volumes[-23:]
    last3 = [v for v in volumes[-3:] if v > 0]
    avg3 = statistics.mean(last3) if last3 else volumes[-1]
    prior20 = vol_window[:-3] if len(vol_window) >= 23 else volumes[-20:]
    avg20 = statistics.mean([v for v in prior20 if v > 0]) if any(v > 0 for v in prior20) else avg3
    volume_ratio = avg3 / avg20 if avg20 else 1.0

    today_ret = pct(today[-1]["close"], today[0]["open"]) if len(today) >= 2 else pct(closes[-1], closes[-2])
    rs_vs_spy = today_ret - spy_today_ret
    sector_alpha = today_ret - sector_ret if sector_ret is not None else 0.0

    range_last = highs[-1] - lows[-1]
    close_pos = (closes[-1] - lows[-1]) / range_last if range_last > 0 else 0.5
    recent_high = max(highs[-20:])
    breakout = cur_close >= recent_high * 0.995
    above_vwap = cur_close > cur_vwap
    atr_pct = (cur_atr / cur_close) * 100 if cur_close else 0

    score = 0.0
    reasons: list[str] = []

    if above_vwap:
        score += 12
        reasons.append("above VWAP")
    else:
        score -= 12
        reasons.append("below VWAP")

    if abs(distance_from_vwap) <= 1.5 and above_vwap:
        score += 8
        reasons.append("controlled VWAP extension")
    elif distance_from_vwap > 2.5:
        score -= 10
        reasons.append("extended above VWAP")

    if volume_ratio >= 2.0:
        score += 18
        reasons.append(f"RVOL {volume_ratio:.2f}x")
    elif volume_ratio >= 1.5:
        score += 12
        reasons.append(f"RVOL {volume_ratio:.2f}x")
    elif volume_ratio >= 1.2:
        score += 6
        reasons.append(f"RVOL {volume_ratio:.2f}x")
    else:
        score -= 8
        reasons.append(f"low RVOL {volume_ratio:.2f}x")

    if bull_stack:
        score += 14
        reasons.append("bull EMA stack")
    elif cur_ema9 > cur_ema21:
        score += 7
        reasons.append("EMA9 above EMA21")
    else:
        score -= 8
        reasons.append("weak EMA stack")

    if rs_vs_spy >= 2.0:
        score += 14
        reasons.append(f"RS vs SPY +{rs_vs_spy:.2f}%")
    elif rs_vs_spy >= 1.0:
        score += 8
        reasons.append(f"RS vs SPY +{rs_vs_spy:.2f}%")
    elif rs_vs_spy >= 0:
        score += 4
        reasons.append(f"RS vs SPY +{rs_vs_spy:.2f}%")
    else:
        score -= 8
        reasons.append(f"lags SPY {rs_vs_spy:.2f}%")

    if sector_alpha >= 1.0:
        score += 8
        reasons.append(f"sector alpha +{sector_alpha:.2f}%")
    elif sector_alpha >= 0:
        score += 4
        reasons.append("sector gate open")
    else:
        score -= 4
        reasons.append("sector lag")

    if breakout:
        score += 10
        reasons.append("near 20-bar high")

    if 0.5 <= atr_pct <= 8:
        score += 8
        reasons.append(f"ATR {atr_pct:.2f}%")
    elif atr_pct < 0.5:
        score -= 6
        reasons.append("low ATR")
    else:
        score -= 6
        reasons.append("high ATR")

    if cur_adx >= 25:
        score += 6
        reasons.append(f"ADX {cur_adx:.1f}")
    if 50 <= cur_rsi <= 75:
        score += 6
        reasons.append(f"RSI {cur_rsi:.1f}")
    elif cur_rsi > 82:
        score -= 8
        reasons.append(f"RSI extended {cur_rsi:.1f}")

    if close_pos >= 0.6:
        score += 6
        reasons.append("strong candle close")
    elif close_pos < 0.4:
        score -= 6
        reasons.append("weak candle close")

    setup_quality = max(0, min(100, round(score)))
    grade = "A" if setup_quality >= 70 else "B" if setup_quality >= 55 else "C" if setup_quality >= 40 else "D"

    return {
        **item,
        "status": "OK",
        "symbol": symbol,
        "setup": "MOMENTUM",
        "setupType": "SURGICAL_STRIKE_STAGE2",
        "setupQualityScore": setup_quality,
        "grade": grade,
        "price": round(cur_close, 4),
        "vwap": round(cur_vwap, 4),
        "distanceFromVWAP": round(distance_from_vwap, 3),
        "volumeRatio": round(volume_ratio, 3),
        "atr_5min": round(cur_atr, 4),
        "atr5MinPct": round(atr_pct, 3),
        "atr14": None,
        "atr_daily": None,
        "atrPct": None,
        "rsVsSpy": round(rs_vs_spy, 3),
        "sectorAlpha": round(sector_alpha, 3),
        "sectorGateOpen": sector_alpha >= 0,
        "momentum": {
            "ema9": round(cur_ema9, 4),
            "ema21": round(cur_ema21, 4),
            "ema50": round(cur_ema50, 4),
            "bullStack": bull_stack,
            "rsi14": round(cur_rsi, 2),
            "adx14": round(cur_adx, 2),
            "closePosition": round(close_pos, 3),
            "near20BarHigh": breakout,
        },
        "entryZone": [round(cur_close * 0.995, 4), round(cur_close * 1.005, 4)],
        "stopLoss": None,
        "riskPerShare": None,
        "stopAtrMultiple": None,
        "stopWarning": None,
        "tp1": None,
        "tp2": None,
        "positionShares": None,
        "positionRiskDollars": None,
        "positionSizingRule": "pending daily ATR14 enrichment on top 40",
        "convictionScore": setup_quality,
        "action": "WATCH",
        "scoreReasons": reasons[:10],
    }


def today_return_from_candles(raw) -> float | None:
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    candles = list(reversed(raw))
    today = get_today(candles)
    if len(today) < 2:
        return None
    return pct(num(today[-1].get("close")), num(today[0].get("open")))


def chunked(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def main() -> None:
    started = time.time()
    api_key = load_fmp_key()
    client = FmpClient(api_key)

    survivors = json.loads(STAGE1_PATH.read_text(encoding="utf-8"))
    no_strc = [s for s in survivors if s.get("symbol") not in BLOCKED_TICKERS]
    STAGE1_NO_STRC_PATH.write_text(json.dumps(no_strc, indent=2), encoding="utf-8")

    symbols = [s["symbol"] for s in no_strc]

    quote_map: dict[str, dict] = {}
    for batch in chunked(symbols, 100):
        data = client.get("stable/batch-quote?symbols=" + ",".join(batch))
        if isinstance(data, list):
            for q in data:
                if q.get("symbol"):
                    quote_map[q["symbol"]] = q

    spy_raw = client.get("stable/historical-chart/5min?symbol=SPY")
    spy_today_ret = today_return_from_candles(spy_raw) or 0.0

    sector_returns: dict[str, float] = {}
    for sector, etf in SECTOR_ETFS.items():
        raw = client.get(f"stable/historical-chart/5min?symbol={etf}")
        ret = today_return_from_candles(raw)
        if ret is not None:
            sector_returns[sector] = ret
    sector_rs_rank = {
        sector: rank
        for rank, (sector, _) in enumerate(
            sorted(sector_returns.items(), key=lambda item: item[1] - spy_today_ret, reverse=True),
            start=1,
        )
    }

    candles_by_symbol: dict[str, list[dict] | None] = {}

    def fetch_symbol(symbol: str):
        return symbol, client.get(f"stable/historical-chart/5min?symbol={symbol}", timeout=30)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_symbol, symbol) for symbol in symbols]
        done = 0
        for future in as_completed(futures):
            symbol, raw = future.result()
            candles_by_symbol[symbol] = raw if isinstance(raw, list) else None
            done += 1
            if done % 100 == 0:
                print(f"Fetched 5min candles for {done}/{len(symbols)}")
                time.sleep(2)

    scored: list[dict] = []
    for item in no_strc:
        symbol = item["symbol"]
        quote = quote_map.get(symbol, {})
        enriched = {**item}
        if quote:
            enriched["price"] = num(quote.get("price"), num(item.get("price")))
            enriched["volume"] = num(quote.get("volume"), num(item.get("volume")))
            enriched["changePercentage"] = num(quote.get("changePercentage"))
            enriched["previousClose"] = num(quote.get("previousClose"))
            enriched["yearHigh"] = num(quote.get("yearHigh"))
            enriched["yearLow"] = num(quote.get("yearLow"))
        enriched["sector_rank"] = sector_rs_rank.get(enriched.get("sector"))
        scored.append(
            score_symbol(
                enriched,
                candles_by_symbol.get(symbol) or [],
                spy_today_ret,
                sector_returns.get(enriched.get("sector", "")),
            )
        )

    eligible = [s for s in scored if s.get("status") == "OK"]
    ranked = sorted(eligible, key=lambda s: (s.get("setupQualityScore", 0), s.get("volumeRatio", 0), s.get("rsVsSpy", 0)), reverse=True)
    top40_pre_risk = ranked[:TOP_N]

    daily_atr_errors: list[str] = []
    enriched_top40: list[dict] = []
    scored_by_symbol = {row.get("symbol"): row for row in scored}
    for setup in top40_pre_risk:
        symbol = setup["symbol"]
        daily_bars = fetch_daily_history(client, symbol)
        enriched = apply_daily_atr_risk(setup, daily_bars)
        if enriched.get("status") != "OK":
            daily_atr_errors.append(f"{symbol}: {enriched.get('stage2RejectReason')}")
        enriched_top40.append(enriched)
        if symbol in scored_by_symbol:
            scored_by_symbol[symbol].update(enriched)
    top40 = [row for row in enriched_top40 if row.get("status") == "OK"]

    # Stage 2 enrichment integrations (post top-40 / post daily ATR)
    top40, enrichment_rejections = apply_stage2_enrichments_and_gates(top40, api_key)
    enriched_all = top40 + enrichment_rejections

    scores = [s.get("setupQualityScore", 0) for s in eligible]
    distribution = {
        "count_ok": len(eligible),
        "score_80_100": sum(1 for s in scores if s >= 80),
        "score_70_79": sum(1 for s in scores if 70 <= s < 80),
        "score_60_69": sum(1 for s in scores if 60 <= s < 70),
        "score_50_59": sum(1 for s in scores if 50 <= s < 60),
        "score_40_49": sum(1 for s in scores if 40 <= s < 50),
        "score_below_40": sum(1 for s in scores if s < 40),
        "min": min(scores) if scores else None,
        "median": statistics.median(scores) if scores else None,
        "max": max(scores) if scores else None,
    }

    metadata = {
        "stage": "B3",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "inputStage1Count": len(survivors),
        "blockedTickersRemoved": sorted(BLOCKED_TICKERS.intersection({s.get("symbol") for s in survivors})),
        "scoredTickerCount": len(no_strc),
        "keptTopN": len(top40),
        "config": {
            "topN": TOP_N,
            "maxWorkers": MAX_WORKERS,
        },
        "fmpCalls": {
            "batchQuoteCalls": math.ceil(len(symbols) / 100),
            "spyIntradayCalls": 1,
            "sectorEtfIntradayCalls": len(SECTOR_ETFS),
            "tickerIntradayCalls": len(symbols),
            "dailyAtrCallsTop40": len(top40_pre_risk),
            "total": client.calls,
        },
        "dailyOhlcvBarsStored": {
            row.get("symbol"): len(row.get("ohlcv_60") or [])
            for row in top40
        },
        "trackAFailCount": sum(bool(row.get("track_a_fail")) for row in top40),
        "trackAFailSymbols": [
            row.get("symbol") for row in top40 if row.get("track_a_fail")
        ],
        "fmpErrorCount": len(client.errors),
        "fmpErrorsSample": client.errors[:20],
        "dailyAtrErrorCount": len(daily_atr_errors),
        "dailyAtrErrorsSample": daily_atr_errors[:20],
        "enrichmentRejectionCount": len(enrichment_rejections),
        "enrichmentRejectionBreakdown": {
            reason: sum(1 for r in enrichment_rejections if r.get("stage2RejectReason") == reason)
            for reason in {r.get("stage2RejectReason") for r in enrichment_rejections}
        },
        "spyTodayReturnPct": round(spy_today_ret, 3),
        "sectorReturnsPct": {k: round(v, 3) for k, v in sorted(sector_returns.items())},
        "scoreDistribution": distribution,
        "runtimeSeconds": round(time.time() - started, 2),
        "notes": [
            "No artificial Stage 1 ranking cut was added.",
            "STRC removed because it is a preferred stock, not common equity.",
            "B3 is ranking/technical filtering only; no brokers, no AI council, no Chronos, no deployment.",
            "5-minute candles are used for VWAP/RVOL/momentum scoring; daily ATR14 is fetched only for top-40 stop sizing.",
            "Stage 2 enrichment integrations applied: Earnings Date Gate, Danelfin Scores, FMP Insider Buying, FMP Shares Float + StockAnalysis Short Interest fallback.",
        ],
    }

    SCORED_PATH.write_text(json.dumps(scored, indent=2), encoding="utf-8")
    TOP40_PATH.write_text(json.dumps(top40, indent=2), encoding="utf-8")
    ENRICHED_ALL_PATH.write_text(json.dumps(enriched_all, indent=2), encoding="utf-8")
    REJECTIONS_PATH.write_text(json.dumps(enrichment_rejections, indent=2), encoding="utf-8")
    META_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(json.dumps({
        "input": len(survivors),
        "scored_after_strc_drop": len(no_strc),
        "top_kept": len(top40),
        "enrichment_rejections": len(enrichment_rejections),
        "fmp_calls": metadata["fmpCalls"],
        "fmp_error_count": len(client.errors),
        "score_distribution": distribution,
        "top_symbols": [s["symbol"] for s in top40[:15]],
        "runtime_seconds": metadata["runtimeSeconds"],
    }, indent=2))


if __name__ == "__main__":
    main()
