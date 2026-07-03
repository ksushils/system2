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
VWMA, relative volume, ATR, RS vs SPY, sector/ETF gate, and momentum.
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

import fmp_cache

try:
    from scoring_engine import has_pending_fda_event
except Exception:
    def has_pending_fda_event(ticker: str, within_days: int = 10) -> bool:
        return False


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
NEWS_CATALYST_PATH = ROOT / "data" / "news_catalyst.json"

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
DAILY_RS_LOOKBACK = 126
DAILY_OHLCV_LOOKBACK = max(60, DAILY_RS_LOOKBACK)
TECHNICAL_SCORE_MAX_RAW = 138
FINALIST_SCORE_THRESHOLD = 55
TOP_N_BY_REGIME = {
    "TRENDING": 60,
    "NORMAL": 50,
    "CHOPPY": 35,
    "CAUTION": 30,
    "RISK_OFF": 20,
}

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
        cacheable_prefixes = (
            "stable/historical-price-eod/full",
            "stable/earnings?",
            "stable/insider-trading?",
            "stable/short-volume?",
            "stable/shares-float?",
        )
        use_daily_cache = endpoint.startswith(cacheable_prefixes)
        if use_daily_cache:
            cached = fmp_cache.get_daily(endpoint)
            if cached is not None:
                return cached
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
                    data = json.loads(raw)
                    if use_daily_cache:
                        fmp_cache.set_daily(endpoint, data)
                    return data
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


def calc_ema(values: list[float], period: int) -> float:
    series = ema(values, period)
    value = series[-1] if series else None
    return float(value) if value is not None else (values[-1] if values else 0.0)


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


def ordered_daily_bars(daily_bars: list[dict]) -> list[dict]:
    if not isinstance(daily_bars, list):
        return []
    rows = [row for row in daily_bars if isinstance(row, dict)]
    return sorted(rows, key=lambda row: str(row.get("date", "")))


def close_return_pct(daily_bars: list[dict], lookback: int) -> float | None:
    bars = ordered_daily_bars(daily_bars)
    if len(bars) <= lookback:
        return None
    start = num(bars[-lookback - 1].get("close"))
    end = num(bars[-1].get("close"))
    if start <= 0 or end <= 0:
        return None
    return pct(end, start)


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1])
    denom = max(1, len(ordered) - 1)
    return {
        symbol: round((idx / denom) * 100, 1)
        for idx, (symbol, _) in enumerate(ordered)
    }


def rs_boost_for_rank(rank: float | None) -> float:
    if rank is None:
        return 0.0
    if rank >= 90:
        return 5.0
    if rank >= 75:
        return 3.0
    if rank >= 60:
        return 1.5
    return 0.0


def sector_boost_for_rank(rank: int | None) -> float:
    if rank is None:
        return 0.0
    return 3.0 if rank <= 3 else 0.0


def breakout_pullback_boost(confirmed: bool) -> float:
    return 4.0 if confirmed else 0.0


def build_swing_quality_signals(
    rows: list[dict],
    daily_by_symbol: dict[str, list[dict] | None],
    spy_daily: list[dict],
    sector_daily: dict[str, list[dict] | None],
) -> tuple[dict[str, dict], dict]:
    spy_returns = {
        "1m": close_return_pct(spy_daily, 21),
        "3m": close_return_pct(spy_daily, 63),
        "6m": close_return_pct(spy_daily, 126),
    }
    composites: dict[str, float] = {}
    raw_rs: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
        if not symbol:
            continue
        bars = daily_by_symbol.get(symbol) or []
        rels: list[float] = []
        detail: dict[str, float | None] = {}
        for label, lookback in (("1m", 21), ("3m", 63), ("6m", 126)):
            ret = close_return_pct(bars, lookback)
            spy_ret = spy_returns.get(label)
            detail[f"return_{label}_pct"] = round(ret, 3) if ret is not None else None
            detail[f"vs_spy_{label}_pct"] = round(ret - spy_ret, 3) if ret is not None and spy_ret is not None else None
            if ret is not None and spy_ret is not None:
                rels.append(ret - spy_ret)
        if rels:
            composite = sum(rels) / len(rels)
            composites[symbol] = composite
            detail["rs_composite_vs_spy_pct"] = round(composite, 3)
        else:
            detail["rs_composite_vs_spy_pct"] = None
        raw_rs[symbol] = detail

    ranks = percentile_ranks(composites)

    sector_strength: dict[str, float] = {}
    for sector, bars in sector_daily.items():
        sector_ret = close_return_pct(bars or [], 21)
        spy_ret = spy_returns.get("1m")
        if sector_ret is not None and spy_ret is not None:
            sector_strength[sector] = sector_ret - spy_ret
    sector_rank = {
        sector: idx
        for idx, (sector, _) in enumerate(
            sorted(sector_strength.items(), key=lambda item: item[1], reverse=True),
            start=1,
        )
    }

    signals: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
        sector = str(row.get("sector") or "").strip()
        rs_rank = ranks.get(symbol)
        breakout = detect_breakout_pullback(daily_by_symbol.get(symbol) or [])
        sector_rank_value = sector_rank.get(sector)
        rs_boost = rs_boost_for_rank(rs_rank)
        sector_boost = sector_boost_for_rank(sector_rank_value)
        bp_boost = breakout_pullback_boost(bool(breakout.get("breakout_pullback_confirmed")))
        signals[symbol] = {
            **raw_rs.get(symbol, {}),
            "rs_rank": rs_rank,
            "rs_rank_source": "1/3/6m_vs_spy_percentile" if rs_rank is not None else "missing_daily_history",
            "rs_rank_boost": rs_boost,
            "sector_strength_rank": sector_rank_value,
            "sector_strength_1m_vs_spy_pct": round(sector_strength[sector], 3) if sector in sector_strength else None,
            "sector_strength_boost": sector_boost,
            "breakout_pullback_confirmed": bool(breakout.get("breakout_pullback_confirmed")),
            "breakout_pullback_detail": breakout,
            "breakout_pullback_boost": bp_boost,
            "quality_signal_boost": round(rs_boost + sector_boost + bp_boost, 2),
        }

    metadata = {
        "spyReturnsPct": {k: round(v, 3) if v is not None else None for k, v in spy_returns.items()},
        "rsRankCoverage": sum(1 for v in signals.values() if v.get("rs_rank") is not None),
        "sectorStrengthRankings": [
            {
                "sector": sector,
                "rank": sector_rank.get(sector),
                "oneMonthVsSpyPct": round(value, 3),
            }
            for sector, value in sorted(sector_strength.items(), key=lambda item: item[1], reverse=True)
        ],
        "breakoutPullbackCount": sum(1 for v in signals.values() if v.get("breakout_pullback_confirmed")),
    }
    return signals, metadata


def detect_breakout_pullback(daily_bars: list[dict]) -> dict:
    bars = ordered_daily_bars(daily_bars)
    if len(bars) < 40:
        return {
            "breakout_pullback_confirmed": False,
            "reason": "insufficient_daily_history",
        }
    volumes = [num(row.get("volume")) for row in bars]
    breakout_idx = None
    breakout_high = None
    breakout_volume_ratio = None
    start_idx = max(30, len(bars) - 10)
    for idx in range(start_idx, len(bars)):
        prior = bars[max(0, idx - 60):idx]
        if len(prior) < 20:
            continue
        prior_high = max(num(row.get("high")) for row in prior)
        avg30 = statistics.mean([v for v in volumes[max(0, idx - 30):idx] if v > 0] or [0])
        vol = num(bars[idx].get("volume"))
        high = num(bars[idx].get("high"))
        near_high = prior_high > 0 and high >= prior_high * 0.995
        volume_breakout = avg30 > 0 and vol >= avg30 * 1.5
        if near_high and volume_breakout:
            breakout_idx = idx
            breakout_high = high
            breakout_volume_ratio = vol / avg30 if avg30 else None

    if breakout_idx is None or breakout_high is None:
        return {
            "breakout_pullback_confirmed": False,
            "reason": "no_recent_high_volume_breakout",
        }
    after = bars[breakout_idx + 1:]
    if not after:
        return {
            "breakout_pullback_confirmed": False,
            "reason": "breakout_has_not_pulled_back_yet",
            "breakout_date": bars[breakout_idx].get("date"),
            "breakout_volume_ratio": round(breakout_volume_ratio, 2) if breakout_volume_ratio else None,
        }
    current_close = num(bars[-1].get("close"))
    pullback_pct = pct(current_close, breakout_high) if breakout_high else 0.0
    post_volumes = [num(row.get("volume")) for row in after if num(row.get("volume")) > 0]
    pre30 = [num(row.get("volume")) for row in bars[max(0, breakout_idx - 30):breakout_idx] if num(row.get("volume")) > 0]
    avg_post = statistics.mean(post_volumes) if post_volumes else None
    avg30 = statistics.mean(pre30) if pre30 else None
    quiet_pullback = bool(avg_post is not None and avg30 and avg_post < avg30 and -12 <= pullback_pct <= -1)
    return {
        "breakout_pullback_confirmed": quiet_pullback,
        "breakout_date": bars[breakout_idx].get("date"),
        "breakout_volume_ratio": round(breakout_volume_ratio, 2) if breakout_volume_ratio else None,
        "pullback_pct_from_breakout_high": round(pullback_pct, 2),
        "post_breakout_volume_vs_30d": round(avg_post / avg30, 2) if avg_post is not None and avg30 else None,
        "reason": "high_vol_breakout_quiet_pullback" if quiet_pullback else "pullback_or_volume_not_quiet",
    }


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


def calc_52week_high_proximity(bars: list[dict]) -> dict | None:
    """Return proximity to the 52-week high from daily OHLCV bars."""
    if len(bars) < 60:
        return None
    lookback = bars[-252:] if len(bars) >= 252 else bars
    high_52wk = max(num(b.get("high")) for b in lookback)
    current = num(bars[-1].get("close"))
    if high_52wk <= 0 or current <= 0:
        return None
    proximity = current / high_52wk
    pct_from_high = (current - high_52wk) / high_52wk * 100
    return {
        "proximity_ratio": round(proximity, 4),
        "pct_from_high": round(pct_from_high, 2),
        "is_new_52wk_high": current >= high_52wk * 0.999,
        "high_52wk": round(high_52wk, 2),
    }


def calc_52week_from_quote(current: float, year_high: float) -> dict | None:
    """Scalable 52-week proximity using FMP batch quote yearHigh."""
    if current <= 0 or year_high <= 0:
        return None
    proximity = current / year_high
    pct_from_high = (current - year_high) / year_high * 100
    return {
        "proximity_ratio": round(proximity, 4),
        "pct_from_high": round(pct_from_high, 2),
        "is_new_52wk_high": current >= year_high * 0.999,
        "high_52wk": round(year_high, 2),
    }


def score_52week_proximity(prox: dict | None) -> int:
    if not prox:
        return 0
    p = num(prox.get("proximity_ratio"))
    if prox.get("is_new_52wk_high"):
        return 25
    if p >= 0.95:
        return 22
    if p >= 0.90:
        return 16
    if p >= 0.85:
        return 10
    if p >= 0.75:
        return 4
    return 0


def calc_adx(bars: list[dict], period: int = 14) -> dict | None:
    """Standard ADX calculation from daily/intraday OHLC bars."""
    if len(bars) < period * 2:
        return None
    trs: list[float] = []
    plus_dms: list[float] = []
    minus_dms: list[float] = []
    for i in range(1, len(bars)):
        h = num(bars[i].get("high"))
        l = num(bars[i].get("low"))
        ph = num(bars[i - 1].get("high"))
        pl = num(bars[i - 1].get("low"))
        pc = num(bars[i - 1].get("close"))
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up_move = h - ph
        down_move = pl - l
        plus_dms.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dms.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    def smooth(vals: list[float], p: int) -> float:
        if not vals:
            return 0.0
        if len(vals) < p:
            return sum(vals) / len(vals)
        return sum(vals[-p:]) / p

    atr_val = smooth(trs, period)
    if atr_val == 0:
        return None
    plus_di = 100 * smooth(plus_dms, period) / atr_val
    minus_di = 100 * smooth(minus_dms, period) / atr_val
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return None
    dx = 100 * abs(plus_di - minus_di) / di_sum
    return {
        "adx": round(dx, 1),
        "plus_di": round(plus_di, 1),
        "minus_di": round(minus_di, 1),
        "bullish": plus_di > minus_di,
    }


def calc_volume_trend(bars: list[dict]) -> dict | None:
    if len(bars) < 10:
        return None
    recent_5 = [num(b.get("volume")) for b in bars[-5:]]
    prior_5 = [num(b.get("volume")) for b in bars[-10:-5]]
    recent_avg = sum(recent_5) / 5
    prior_avg = sum(prior_5) / 5 if sum(prior_5) else 1
    vol_trend = recent_avg / prior_avg
    return {
        "vol_trend_ratio": round(vol_trend, 2),
        "rising": vol_trend > 1.2,
    }


def detect_pullback(bars: list[dict]) -> dict | None:
    """Detect an uptrend pullback into EMA21 support."""
    if len(bars) < 30:
        return None
    closes = [num(b.get("close")) for b in bars]
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    current = num(bars[-1].get("close"))
    if ema21 <= 0 or current <= 0:
        return None
    uptrend = ema21 > ema50
    recent_high = max(num(b.get("high")) for b in bars[-10:])
    pullback_pct = (current - recent_high) / recent_high * 100 if recent_high else 0.0
    near_ema21 = abs(current - ema21) / ema21 < 0.02
    is_pullback = uptrend and -10 <= pullback_pct <= -3 and near_ema21
    return {
        "is_pullback_setup": is_pullback,
        "pullback_pct": round(pullback_pct, 1),
        "near_ema21_support": near_ema21,
    }


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


def vwma(candles: list[dict]) -> float:
    # VWMA: volume-weighted moving average over candles. NOT intraday VWAP.
    # Measures price vs volume-weighted fair value over the selected window.
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


def detect_market_regime() -> str:
    regime = str(os.environ.get("SYSTEM2_REGIME") or "").strip().upper()
    if regime in TOP_N_BY_REGIME:
        return regime
    regime_path = ROOT / "regime_check_latest.json"
    if regime_path.exists():
        try:
            data = json.loads(regime_path.read_text(encoding="utf-8"))
            regime = str(data.get("regime") or data.get("verdict") or "").strip().upper()
            if regime in TOP_N_BY_REGIME:
                return regime
        except Exception:
            pass
    return "NORMAL"


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
        if isinstance(data, dict) and "scores" in data:
            return data["scores"]
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


def fetch_fmp_recent_news(ticker: str, fmp_key: str, days: int = 21) -> list[dict]:
    """Fetch recent news from FMP. Fail-open."""
    client = FmpClient(fmp_key)
    try:
        data = client.get(f"stable/news?symbol={urllib.parse.quote(ticker)}&page=0&limit=20", timeout=10)
        if isinstance(data, list):
            cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
            filtered = []
            for a in data:
                d = a.get("publishedDate") or a.get("date")
                if not d:
                    continue
                try:
                    ad = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                except Exception:
                    continue
                if ad >= cutoff:
                    filtered.append(a)
            return filtered
    except Exception:
        pass
    return []


def load_news_catalyst() -> dict:
    """Load today's NewsAPI/Alpha Vantage catalyst verdicts."""
    if not NEWS_CATALYST_PATH.exists():
        return {}
    try:
        data = json.loads(NEWS_CATALYST_PATH.read_text(encoding="utf-8"))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if data.get("date") == today:
            return data.get("results", {})
    except Exception:
        pass
    return {}


def check_news_safety(ticker: str, news_data: dict) -> tuple[bool, str, dict]:
    news = news_data.get(ticker, {}) if isinstance(news_data, dict) else {}
    verdict = str(news.get("news_verdict", "NO_DATA")).upper()
    if verdict == "DANGER":
        return False, "news_danger_detected", news
    if verdict == "CAUTION":
        return True, "news_caution", news
    return True, verdict, news


def apply_stage2_enrichments_and_gates(rows: list[dict], fmp_key: str) -> tuple[list[dict], list[dict]]:
    """Run Stage 2 enrichment integrations after top-40 selection. Returns (pass, reject)."""
    danelfin_scores = load_danelfin_scores()
    stockanalysis_scores = load_stockanalysis_scores()
    news_catalyst = load_news_catalyst()
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
        news_ok, news_reason, news = check_news_safety(symbol, news_catalyst)
        merged["news_verdict"] = news.get("news_verdict", "NO_DATA")
        merged["news_sentiment_score"] = news.get("news_sentiment_score", 0)
        merged["news_headline"] = news.get("best_headline", "")
        merged["news_article_count"] = news.get("article_count", 0)
        merged["news_sources_used"] = news.get("sources_used", [])
        merged["fda_event_flag"] = False
        rejected = False

        if has_pending_fda_event(symbol, within_days=10):
            merged["status"] = "REJECTED"
            merged["stage2RejectReason"] = "pending_fda_event"
            merged["fda_event_flag"] = True
            merged["rejectReasons"] = list(merged.get("rejectReasons") or []) + ["pending_fda_event"]
            rejected_rows.append(merged)
            rejected = True
            print(f"{symbol} REJECTED: Finnhub FDA event pending within 10 days")

        if not rejected and not news_ok:
            reason = news_reason
            merged["status"] = "REJECTED"
            merged["stage2RejectReason"] = reason
            merged["rejectReasons"] = list(row.get("rejectReasons") or []) + [reason]
            rejected_rows.append(merged)
            rejected = True
        elif news_reason == "news_caution":
            merged["news_caution_flag"] = True
            merged["risk_score"] = merged.get("risk_score", 0) + 5

        if not rejected and earnings.get("earnings_risk") == "DANGER":
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

        # ── Forecast-cone-gated STRONG_DOWN ─────────────────────────────
        if not rejected:
            combined_forecast = str(merged.get("combined_forecast_dir") or "").strip().upper()
            # Derive cone category from numeric combined_band_pct
            # TIGHT: < 3%, MODERATE: 3-5%, WIDE: > 5%
            band = num(merged.get("combined_band_pct"))
            if combined_forecast == "STRONG_DOWN":
                if band is not None and band < 5:
                    # TIGHT or MODERATE cone = high conviction bearish → reject
                    merged["status"] = "REJECTED"
                    merged["stage2RejectReason"] = "strong_down_tight_cone"
                    merged["rejectReasons"] = list(merged.get("rejectReasons") or []) + ["strong_down_tight_cone"]
                    rejected_rows.append(merged)
                    rejected = True
                    print(f"{symbol}: REJECTED — STRONG_DOWN with tight/moderate cone ({band:.1f}%)")
                elif band is not None and band >= 5:
                    # WIDE cone = low conviction → pass with extra risk flag
                    merged["wide_cone_strong_down_flag"] = True
                    merged["risk_score"] = merged.get("risk_score", 0) + 8
                    print(f"{symbol}: STRONG_DOWN but WIDE cone ({band:.1f}%) — risk +8, not rejected")
                else:
                    # Unknown cone width — conservative reject
                    merged["status"] = "REJECTED"
                    merged["stage2RejectReason"] = "strong_down_cone_unknown"
                    merged["rejectReasons"] = list(merged.get("rejectReasons") or []) + ["strong_down_cone_unknown"]
                    rejected_rows.append(merged)
                    rejected = True
                    print(f"{symbol}: REJECTED — STRONG_DOWN, cone unknown — conservative reject")

        # ── Healthcare / Biotech binary event gate ───────────────────────
        if not rejected:
            sector = str(merged.get("sector") or "").strip()
            if sector in {"Healthcare", "Biotechnology", "Pharmaceutical", "Biopharmaceutical"}:
                fda_keywords = [
                    "fda", "pdufa", "nda", "bla",
                    "clinical trial", "phase 3", "phase 2",
                    "phase iii", "phase ii",
                    "trial results", "trial data",
                    "approval decision", "adcom",
                    "advisory committee", "regulatory decision",
                    "complete response letter", "crl",
                ]
                try:
                    combined_text = " ".join([
                        str(merged.get("news_headline") or ""),
                        str(merged.get("news_verdict") or ""),
                    ]).lower()
                    fda_event_detected = any(kw in combined_text for kw in fda_keywords)
                except Exception as e:
                    fda_event_detected = False
                    merged["fda_gate_skipped"] = True
                    merged["fda_gate_skip_reason"] = str(e)
                    print(f"{symbol} Healthcare FDA gate skipped ({e})")

                if fda_event_detected:
                    merged["status"] = "REJECTED"
                    merged["stage2RejectReason"] = "healthcare_binary_event"
                    merged["fda_event_risk"] = True
                    merged["rejectReasons"] = list(merged.get("rejectReasons") or []) + ["healthcare_binary_event"]
                    rejected_rows.append(merged)
                    rejected = True
                    print(f"{symbol} REJECTED: Healthcare binary event detected in news")

        if not rejected:
            enriched_rows.append(merged)

    print(f"Earnings rejects: {earnings_rejects}")
    print(f"Danelfin rejects: {danelfin_rejects}")
    print(f"Enriched: {len(enriched_rows)}")
    return enriched_rows, rejected_rows


def score_symbol(
    item: dict,
    candles_raw: list[dict],
    spy_today_ret: float,
    sector_ret: float | None,
    quality_signals: dict | None = None,
) -> dict:
    symbol = item["symbol"]
    quality_signals = quality_signals or {}
    if not isinstance(candles_raw, list) or len(candles_raw) < 30:
        return {
            **item,
            **quality_signals,
            "status": "REJECTED",
            "stage2RejectReason": "insufficient_5min_candles",
            "setupQualityScore": 0,
            "setup_score_base": 0,
        }

    candles = list(reversed(candles_raw))
    closes = [num(c.get("close")) for c in candles]
    highs = [num(c.get("high")) for c in candles]
    lows = [num(c.get("low")) for c in candles]
    volumes = [num(c.get("volume")) for c in candles]

    today = get_today(candles)
    vwap_candles = today if len(today) >= 5 else candles[-78:]
    cur_close = closes[-1]
    cur_vwma = vwma(vwap_candles)
    distance_from_vwma = pct(cur_close, cur_vwma)

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

    # RVOL quality tier
    if volume_ratio >= 5.0:
        rvol_tier = "EXTREME"
    elif volume_ratio >= 3.0:
        rvol_tier = "HIGH"
    elif volume_ratio >= 2.0:
        rvol_tier = "ELEVATED"
    else:
        rvol_tier = "LOW"

    prox = calc_52week_from_quote(cur_close, num(item.get("yearHigh")))
    fiftytwo_score = score_52week_proximity(prox)

    adx_data = calc_adx(candles, 14)
    adx_score = 0
    adx_bullish = False
    if adx_data:
        adx_val = num(adx_data.get("adx"))
        adx_bullish = bool(adx_data.get("bullish"))
        if adx_val > 30 and adx_bullish:
            adx_score = 12
        elif adx_val > 25 and adx_bullish:
            adx_score = 8
        elif adx_val > 20 and adx_bullish:
            adx_score = 4

    vt = calc_volume_trend(candles)
    vol_trend_score = 0
    if vt:
        if vt["vol_trend_ratio"] > 1.5:
            vol_trend_score = 8
        elif vt["vol_trend_ratio"] > 1.2:
            vol_trend_score = 4

    pb = detect_pullback(candles)
    pullback_score = 12 if pb and pb.get("is_pullback_setup") else 0
    setup_type = "PULLBACK" if pullback_score else "MOMENTUM"
    if prox and prox.get("is_new_52wk_high"):
        setup_type = "BREAKOUT"

    # ── RVOL minimum gate (raised to 2.0x based on backtest validation) ──
    if volume_ratio < 2.0:
        return {
            **item,
            **quality_signals,
            "status": "REJECTED",
            "stage2RejectReason": "rvol_below_2x_minimum",
            "rejectReasons": list(item.get("rejectReasons") or []) + ["rvol_below_2x_minimum"],
            "setupQualityScore": 0,
            "setup_score_base": 0,
            "volumeRatio": round(volume_ratio, 3),
            "rvol": round(volume_ratio, 3),
            "rvol_tier": rvol_tier,
            "proximity_52wk": prox.get("proximity_ratio") if prox else None,
            "pct_from_52wk_high": prox.get("pct_from_high") if prox else None,
            "is_new_52wk_high": prox.get("is_new_52wk_high") if prox else False,
            "high_52wk": prox.get("high_52wk") if prox else None,
            "fiftytwo_score": fiftytwo_score,
            "adx": adx_data.get("adx") if adx_data else None,
            "adx_bullish": adx_bullish,
            "adx_score": adx_score,
            "vol_trend_ratio": vt.get("vol_trend_ratio") if vt else None,
            "vol_trend_score": vol_trend_score,
            "is_pullback_setup": bool(pb and pb.get("is_pullback_setup")),
            "pullback_pct": pb.get("pullback_pct") if pb else None,
            "near_ema21_support": pb.get("near_ema21_support") if pb else None,
            "pullback_score": pullback_score,
            "setup_type": setup_type,
            "vwma": round(cur_vwma, 4),
            "vwma_pct": round(distance_from_vwma, 3),
            "distanceFromVWMA": round(distance_from_vwma, 3),
            "vwap": round(cur_vwma, 4),
            "distanceFromVWAP": round(distance_from_vwma, 3),
        }

    today_ret = pct(today[-1]["close"], today[0]["open"]) if len(today) >= 2 else pct(closes[-1], closes[-2])
    rs_vs_spy = today_ret - spy_today_ret
    sector_alpha = today_ret - sector_ret if sector_ret is not None else 0.0

    range_last = highs[-1] - lows[-1]
    close_pos = (closes[-1] - lows[-1]) / range_last if range_last > 0 else 0.5
    recent_high = max(highs[-20:])
    breakout = cur_close >= recent_high * 0.995
    above_vwma = cur_close > cur_vwma
    atr_pct = (cur_atr / cur_close) * 100 if cur_close else 0

    rvol_score = 25 if volume_ratio >= 2.5 else 20 if volume_ratio >= 2.0 else 0
    rs_score = 20 if rs_vs_spy >= 2.0 else 14 if rs_vs_spy >= 1.0 else 8 if rs_vs_spy >= 0 else 0
    ema_stack_score = 15 if bull_stack else 8 if cur_ema9 > cur_ema21 else 0
    vwma_score = 8 if above_vwma and abs(distance_from_vwma) <= 1.5 else 5 if above_vwma else 0
    sector_rs_score = 8 if sector_alpha >= 1.0 else 4 if sector_alpha >= 0 else 0
    atr_quality_score = 5 if 0.5 <= atr_pct <= 8 else 0
    raw_score = (
        fiftytwo_score
        + rvol_score
        + rs_score
        + ema_stack_score
        + adx_score
        + pullback_score
        + vol_trend_score
        + vwma_score
        + sector_rs_score
        + atr_quality_score
    )
    normalized_score_base = round(raw_score / TECHNICAL_SCORE_MAX_RAW * 100, 2)
    quality_signal_boost = num(quality_signals.get("quality_signal_boost"))
    normalized_score = round(min(100.0, normalized_score_base + quality_signal_boost), 2)

    reasons: list[str] = []
    if prox:
        if prox.get("is_new_52wk_high"):
            reasons.append("new 52-week high")
        elif prox["proximity_ratio"] >= 0.90:
            reasons.append(f"{abs(prox['pct_from_high']):.1f}% from 52-week high")
    reasons.append(f"RVOL {volume_ratio:.2f}x")

    if bull_stack:
        reasons.append("bull EMA stack")
    elif cur_ema9 > cur_ema21:
        reasons.append("EMA9 above EMA21")
    else:
        reasons.append("weak EMA stack")

    if rs_vs_spy >= 2.0:
        reasons.append(f"RS vs SPY +{rs_vs_spy:.2f}%")
    elif rs_vs_spy >= 1.0:
        reasons.append(f"RS vs SPY +{rs_vs_spy:.2f}%")
    elif rs_vs_spy >= 0:
        reasons.append(f"RS vs SPY +{rs_vs_spy:.2f}%")
    else:
        reasons.append(f"lags SPY {rs_vs_spy:.2f}%")

    if sector_alpha >= 1.0:
        reasons.append(f"sector alpha +{sector_alpha:.2f}%")
    elif sector_alpha >= 0:
        reasons.append("sector gate open")
    else:
        reasons.append("sector lag")

    if breakout:
        reasons.append("near 20-bar high")

    if 0.5 <= atr_pct <= 8:
        reasons.append(f"ATR {atr_pct:.2f}%")
    elif atr_pct < 0.5:
        reasons.append("low ATR")
    else:
        reasons.append("high ATR")

    if adx_data:
        reasons.append(f"ADX {adx_data['adx']:.1f}{' bullish' if adx_bullish else ' bearish'}")
    if vt and vt.get("rising"):
        reasons.append(f"volume trend {vt['vol_trend_ratio']:.2f}x")
    if pb and pb.get("is_pullback_setup"):
        reasons.append("pullback to EMA21 support")
    if quality_signals.get("rs_rank") is not None:
        reasons.append(f"swing RS rank {quality_signals['rs_rank']:.1f}")
    if quality_signals.get("sector_strength_rank") is not None and quality_signals.get("sector_strength_rank") <= 3:
        reasons.append(f"top-{quality_signals['sector_strength_rank']} sector")
    if quality_signals.get("breakout_pullback_confirmed"):
        reasons.append("high-vol breakout, quiet pullback")
    if above_vwma:
        reasons.append("above VWMA")
    else:
        reasons.append("below VWMA")

    if 50 <= cur_rsi <= 75:
        reasons.append(f"RSI {cur_rsi:.1f}")
    elif cur_rsi > 82:
        reasons.append(f"RSI extended {cur_rsi:.1f}")

    if close_pos >= 0.6:
        reasons.append("strong candle close")
    elif close_pos < 0.4:
        reasons.append("weak candle close")

    if normalized_score < FINALIST_SCORE_THRESHOLD:
        return {
            **item,
            **quality_signals,
            "status": "REJECTED",
            "stage2RejectReason": "setup_score_below_55",
            "rejectReasons": list(item.get("rejectReasons") or []) + ["setup_score_below_55"],
            "setupQualityScore": normalized_score,
            "setup_score": normalized_score,
            "setup_score_base": normalized_score_base,
            "setup_score_raw": raw_score,
            "volumeRatio": round(volume_ratio, 3),
            "rvol": round(volume_ratio, 3),
            "rvol_tier": rvol_tier,
            "rsVsSpy": round(rs_vs_spy, 3),
            "rs_vs_spy": round(rs_vs_spy, 3),
            "proximity_52wk": prox.get("proximity_ratio") if prox else None,
            "pct_from_52wk_high": prox.get("pct_from_high") if prox else None,
            "is_new_52wk_high": prox.get("is_new_52wk_high") if prox else False,
            "high_52wk": prox.get("high_52wk") if prox else None,
            "fiftytwo_score": fiftytwo_score,
            "adx": adx_data.get("adx") if adx_data else None,
            "adx_bullish": adx_bullish,
            "adx_score": adx_score,
            "vol_trend_ratio": vt.get("vol_trend_ratio") if vt else None,
            "vol_trend_score": vol_trend_score,
            "is_pullback_setup": bool(pb and pb.get("is_pullback_setup")),
            "pullback_pct": pb.get("pullback_pct") if pb else None,
            "near_ema21_support": pb.get("near_ema21_support") if pb else None,
            "pullback_score": pullback_score,
            "setup_type": setup_type,
            "vwma": round(cur_vwma, 4),
            "vwma_pct": round(distance_from_vwma, 3),
            "distanceFromVWMA": round(distance_from_vwma, 3),
            "technical_score_breakdown": {
                "fiftytwo_score": fiftytwo_score,
                "rvol_score": rvol_score,
                "rs_score": rs_score,
                "ema_stack_score": ema_stack_score,
                "adx_score": adx_score,
                "pullback_score": pullback_score,
                "vol_trend_score": vol_trend_score,
                "vwma_score": vwma_score,
                "sector_rs_score": sector_rs_score,
                "atr_quality_score": atr_quality_score,
                "rs_rank_boost": quality_signals.get("rs_rank_boost", 0),
                "sector_strength_boost": quality_signals.get("sector_strength_boost", 0),
                "breakout_pullback_boost": quality_signals.get("breakout_pullback_boost", 0),
                "quality_signal_boost": quality_signal_boost,
                "max_raw": TECHNICAL_SCORE_MAX_RAW,
            },
            "scoreReasons": reasons[:10],
        }

    setup_quality = normalized_score
    grade = "A" if setup_quality >= 70 else "B" if setup_quality >= 55 else "C" if setup_quality >= 40 else "D"

    return {
            **item,
            **quality_signals,
            "status": "OK",
            "symbol": symbol,
        "setup": setup_type,
        "setupType": setup_type,
        "setup_type": setup_type,
        "setupQualityScore": setup_quality,
        "setup_score": setup_quality,
        "setup_score_base": normalized_score_base,
        "setup_score_raw": raw_score,
        "grade": grade,
        "price": round(cur_close, 4),
        "vwma": round(cur_vwma, 4),
        "vwma_pct": round(distance_from_vwma, 3),
        "distanceFromVWMA": round(distance_from_vwma, 3),
        "vwap": round(cur_vwma, 4),
        "distanceFromVWAP": round(distance_from_vwma, 3),
        "volumeRatio": round(volume_ratio, 3),
        "rvol": round(volume_ratio, 3),
        "rvol_tier": rvol_tier,
        "atr_5min": round(cur_atr, 4),
        "atr5MinPct": round(atr_pct, 3),
        "atr14": None,
        "atr_daily": None,
        "atrPct": None,
        "rsVsSpy": round(rs_vs_spy, 3),
        "rs_vs_spy": round(rs_vs_spy, 3),
        "sectorAlpha": round(sector_alpha, 3),
        "sector_rs": round(sector_alpha, 3),
        "sectorGateOpen": sector_alpha >= 0,
        "proximity_52wk": prox.get("proximity_ratio") if prox else None,
        "pct_from_52wk_high": prox.get("pct_from_high") if prox else None,
        "is_new_52wk_high": prox.get("is_new_52wk_high") if prox else False,
        "high_52wk": prox.get("high_52wk") if prox else None,
        "fiftytwo_score": fiftytwo_score,
        "adx": adx_data.get("adx") if adx_data else None,
        "adx_bullish": adx_bullish,
        "adx_score": adx_score,
        "vol_trend_ratio": vt.get("vol_trend_ratio") if vt else None,
        "vol_trend_score": vol_trend_score,
        "is_pullback_setup": bool(pb and pb.get("is_pullback_setup")),
        "pullback_pct": pb.get("pullback_pct") if pb else None,
        "near_ema21_support": pb.get("near_ema21_support") if pb else None,
        "pullback_score": pullback_score,
        "technical_score_breakdown": {
            "fiftytwo_score": fiftytwo_score,
            "rvol_score": rvol_score,
            "rs_score": rs_score,
            "ema_stack_score": ema_stack_score,
            "adx_score": adx_score,
            "pullback_score": pullback_score,
            "vol_trend_score": vol_trend_score,
            "vwma_score": vwma_score,
            "sector_rs_score": sector_rs_score,
            "atr_quality_score": atr_quality_score,
            "rs_rank_boost": quality_signals.get("rs_rank_boost", 0),
            "sector_strength_boost": quality_signals.get("sector_strength_boost", 0),
            "breakout_pullback_boost": quality_signals.get("breakout_pullback_boost", 0),
            "quality_signal_boost": quality_signal_boost,
            "max_raw": TECHNICAL_SCORE_MAX_RAW,
        },
        "momentum": {
            "ema9": round(cur_ema9, 4),
            "ema21": round(cur_ema21, 4),
            "ema50": round(cur_ema50, 4),
            "bullStack": bull_stack,
            "rsi14": round(cur_rsi, 2),
            "adx14": round(adx_data.get("adx") if adx_data else cur_adx, 2),
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
    market_regime = detect_market_regime()
    top_n = TOP_N_BY_REGIME.get(market_regime, TOP_N)
    print(f"Regime {market_regime}: keeping top {top_n}")

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

    daily_by_symbol: dict[str, list[dict] | None] = {}

    def fetch_daily_symbol(symbol: str):
        return symbol, fetch_daily_history(client, symbol)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_daily_symbol, symbol) for symbol in symbols]
        done = 0
        for future in as_completed(futures):
            symbol, raw = future.result()
            daily_by_symbol[symbol] = raw if isinstance(raw, list) else None
            done += 1
            if done % 100 == 0:
                print(f"Fetched daily candles for {done}/{len(symbols)}")
                time.sleep(2)

    spy_daily = fetch_daily_history(client, "SPY")
    sector_daily: dict[str, list[dict] | None] = {}
    for sector, etf in SECTOR_ETFS.items():
        sector_daily[sector] = fetch_daily_history(client, etf)
    quality_signal_by_symbol, quality_signal_metadata = build_swing_quality_signals(
        no_strc,
        daily_by_symbol,
        spy_daily,
        sector_daily,
    )

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
                quality_signal_by_symbol.get(symbol, {}),
            )
        )

    eligible = [s for s in scored if s.get("status") == "OK"]
    ranked = sorted(
        eligible,
        key=lambda s: (
            s.get("setup_score", s.get("setupQualityScore", 0)),
            s.get("rs_rank") if s.get("rs_rank") is not None else -1,
            s.get("volumeRatio", 0),
            s.get("rsVsSpy", 0),
        ),
        reverse=True,
    )
    top40_pre_risk = ranked[:top_n]

    daily_atr_errors: list[str] = []
    enriched_top40: list[dict] = []
    scored_by_symbol = {row.get("symbol"): row for row in scored}
    for setup in top40_pre_risk:
        symbol = setup["symbol"]
        daily_bars = daily_by_symbol.get(symbol) or []
        enriched = apply_daily_atr_risk(setup, daily_bars)
        if enriched.get("status") != "OK":
            daily_atr_errors.append(f"{symbol}: {enriched.get('stage2RejectReason')}")
        enriched_top40.append(enriched)
        if symbol in scored_by_symbol:
            scored_by_symbol[symbol].update(enriched)
    top40 = [row for row in enriched_top40 if row.get("status") == "OK"]

    # Mark Set 1 origin
    for row in top40:
        row["set"] = 1
        row["set_source"] = "technical_momentum"

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
            "topN": top_n,
            "configuredDefaultTopN": TOP_N,
            "topNByRegime": TOP_N_BY_REGIME,
            "detectedMarketRegime": market_regime,
            "finalistScoreThreshold": FINALIST_SCORE_THRESHOLD,
            "technicalScoreMaxRaw": TECHNICAL_SCORE_MAX_RAW,
            "maxWorkers": MAX_WORKERS,
        },
        "fmpCalls": {
            "batchQuoteCalls": math.ceil(len(symbols) / 100),
            "spyIntradayCalls": 1,
            "sectorEtfIntradayCalls": len(SECTOR_ETFS),
            "tickerIntradayCalls": len(symbols),
            "dailyHistoryCallsStage1": len(symbols),
            "dailyHistoryCallsSpyAndSectorEtfs": 1 + len(SECTOR_ETFS),
            "dailyAtrCallsTop40": 0,
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
        "qualitySignals": quality_signal_metadata,
        "scoreDistribution": distribution,
        "runtimeSeconds": round(time.time() - started, 2),
        "notes": [
            "No artificial Stage 1 ranking cut was added.",
            "STRC removed because it is a preferred stock, not common equity.",
            "B3 is ranking/technical filtering only; no brokers, no AI council, no Chronos, no deployment.",
            "Technical scoring is normalized from a 138-point research-weighted raw model; RVOL below 2.0x is a hard gate and setup_score below 55 is rejected.",
            "5-minute candles are used for VWMA/RVOL/momentum scoring; daily ATR14 is fetched only for selected top-N stop sizing.",
            "VWMA is a volume-weighted moving average over candles, not intraday VWAP.",
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
