#!/usr/bin/env python3
"""
System 2 — 3-Category Trade Quality Scoring Engine.

Computes:
  core_setup_score      (0-100) — technical setup quality
  confirmation_score    (0-100) — supporting signal strength
  risk_score            (0-100) — risk factors (higher = more risk)
  trade_quality_score   (0-100) — weighted composite
  trade_quality_label   — human-readable grade
  data_quality_score    (0-100) — data source freshness/availability
  bear_case_points      — list of risk warnings for display

Designed for paper mode. Scores are advisory only until the scoring
loop proves edge.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


# ═══════════════════════════════════════════════════════════════════════════════
# SELF-LEARNING WEIGHT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Theory weights for the NORMAL regime. These are the baseline the live system
# uses today. The self-learning engine may nudge the family caps once it has
# enough resolved trades, but it always returns to these weights when dormant.
THEORY_WEIGHTS: dict[str, float] = {
    "momentum": 25.0,
    "positioning": 35.0,
    "catalyst": 20.0,
    "structural": 20.0,
    "core_weight": 0.50,
    "confirmation_weight": 0.30,
    "risk_weight": 0.20,
}


def load_config() -> dict[str, Any]:
    """Load system config; default to self-learning enabled."""
    default = {"self_learning_enabled": True}
    path = ROOT / "config.json"
    if not path.exists():
        return default.copy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default.copy()
        return {**default, **data}
    except Exception:
        return default.copy()


def get_active_weights() -> dict[str, float]:
    """
    Returns theory weights unless self-learning is active AND past the
    dormancy gate. This is the single integration point for learned weights.
    """
    config = load_config()
    if not config.get("self_learning_enabled", True):
        return THEORY_WEIGHTS.copy()

    lw_path = ROOT / "data" / "learned_weights.json"
    if not lw_path.exists():
        return THEORY_WEIGHTS.copy()

    try:
        lw = json.loads(lw_path.read_text(encoding="utf-8"))
    except Exception:
        return THEORY_WEIGHTS.copy()

    if lw.get("mode") == "DORMANT":
        return THEORY_WEIGHTS.copy()

    # Log active mode for transparency in the pipeline log
    print(
        f"Scoring with {lw.get('mode')} learned weights "
        f"({lw.get('total_resolved')} trades, cap={lw.get('cap')})"
    )
    return dict(lw.get("weights", THEORY_WEIGHTS))


def text(value: Any) -> str:
    return str(value or "").strip()


def number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def num(value: Any, default: float = 0.0) -> float:
    return number(value) or default


# ═══════════════════════════════════════════════════════════════════════════════
# REGIME DETECTION & ADAPTIVE CAPS
# ═══════════════════════════════════════════════════════════════════════════════

def detect_market_regime(row: dict[str, Any]) -> str:
    """Detect market regime from available data sources.

    Priority:
      1. VIX regime (most reliable, already computed by pipeline)
      2. GEX regime (FlashAlpha gamma exposure)
      3. SPY 5-day return (momentum context)
      4. Environment variable fallback (confluence_scoring.py passthrough)
      5. Default NORMAL
    """
    import os

    # VIX-based regime (highest priority — already validated by pipeline)
    vix_regime = text(row.get("vix_regime")).upper()
    if vix_regime == "RISK_OFF":
        return "RISK_OFF"
    if vix_regime == "CAUTION":
        return "CAUTION"
    # VIX NORMAL or TRENDING: continue to GEX refinement below

    # Fallback: market_regime or regime on the row
    row_regime = text(row.get("market_regime") or row.get("regime")).upper()
    if row_regime == "RISK_OFF":
        return "RISK_OFF"
    if row_regime == "CAUTION":
        return "CAUTION"
    if row_regime == "TRENDING":
        return "TRENDING"

    # Environment fallback (used by confluence_scoring.py)
    env_regime = text(os.environ.get("SYSTEM2_REGIME")).upper()
    if env_regime == "RISK_OFF":
        return "RISK_OFF"
    if env_regime == "CAUTION":
        return "CAUTION"
    if env_regime == "TRENDING":
        return "TRENDING"

    # GEX-based refinement for NORMAL cases
    gex_regime = text(row.get("gex_regime")).upper()
    spy_rs = num(row.get("spy_rs_5d"), 0)

    if gex_regime in {"BULLISH", "ABOVE_FLIP", "POSITIVE"} and spy_rs > 1.0:
        return "TRENDING"
    if gex_regime in {"BEARISH", "BELOW_FLIP", "NEGATIVE", "NEGATIVE_GAMMA"}:
        return "CHOPPY"

    # If VIX or row or env said TRENDING, respect it
    if vix_regime == "TRENDING" or row_regime == "TRENDING" or env_regime == "TRENDING":
        return "TRENDING"

    return "NORMAL"


def get_regime_caps(regime: str) -> dict[str, float]:
    """Return regime-adjusted family caps and trade-quality weights.

    Weights must sum to 1.0:
      core_weight + confirmation_weight + risk_weight == 1.0

    Self-learning nudges only the NORMAL-regime base weights. Regime-specific
    overrides (RISK_OFF, CAUTION, TRENDING, CHOPPY) remain theory-based so
    the system does not overfit rare macro states.
    """
    # Theory base is always safe.
    theory_base = THEORY_WEIGHTS.copy()

    if regime == "RISK_OFF":
        return {
            **theory_base,
            "momentum": 10.0,
            "positioning": 35.0,
            "catalyst": 15.0,
            "structural": 20.0,
            "core_weight": 0.35,
            "confirmation_weight": 0.40,
            "risk_weight": 0.25,
        }

    if regime == "CAUTION":
        return {
            **theory_base,
            "momentum": 18.0,
            "positioning": 35.0,
            "catalyst": 20.0,
            "structural": 20.0,
            "core_weight": 0.45,
            "confirmation_weight": 0.35,
            "risk_weight": 0.20,
        }

    if regime == "TRENDING":
        return {
            **theory_base,
            "momentum": 30.0,
            "positioning": 35.0,
            "catalyst": 20.0,
            "structural": 15.0,
            "core_weight": 0.55,
            "confirmation_weight": 0.28,
            "risk_weight": 0.17,
        }

    if regime == "CHOPPY":
        return {
            **theory_base,
            "momentum": 15.0,
            "positioning": 35.0,
            "catalyst": 25.0,
            "structural": 20.0,
            "core_weight": 0.40,
            "confirmation_weight": 0.38,
            "risk_weight": 0.22,
        }

    # NORMAL — allow self-learning to nudge the base weights
    return get_active_weights().copy()


# ═══════════════════════════════════════════════════════════════════════════════
# DANELFIN LOOKUP (cached)
# ═══════════════════════════════════════════════════════════════════════════════

def load_danelfin_scores() -> dict[str, dict]:
    path = Path("/root/system2-core/data/danelfin_scores.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("scores", {})
    except Exception:
        return {}

_DANELFIN_SCORES: dict[str, dict] = {}
_GEX_REGIME: dict[str, Any] = {}
_NEWS_CATALYST: dict[str, Any] = {}
_FINALIST_OPTIONS: dict[str, Any] = {}
_EARNINGS_DRIFT: dict[str, Any] = {}
_FINNHUB_DATA: dict[str, Any] | None = None


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def get_danelfin_score(ticker: str) -> dict | None:
    global _DANELFIN_SCORES
    if not _DANELFIN_SCORES:
        _DANELFIN_SCORES = load_danelfin_scores()
    return _DANELFIN_SCORES.get(ticker.upper())


def load_gex_regime() -> dict[str, Any]:
    path = ROOT / "data" / "gex_regime.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_gex_regime_data() -> dict[str, Any]:
    global _GEX_REGIME
    if not _GEX_REGIME:
        _GEX_REGIME = load_gex_regime()
    return _GEX_REGIME


def load_news_catalyst() -> dict[str, Any]:
    path = ROOT / "data" / "news_catalyst.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("date") == utc_today():
            return data.get("results", {})
    except Exception:
        pass
    return {}


def get_news_catalyst(ticker: str) -> dict[str, Any]:
    global _NEWS_CATALYST
    if not _NEWS_CATALYST:
        _NEWS_CATALYST = load_news_catalyst()
    value = _NEWS_CATALYST.get(ticker.upper())
    return value if isinstance(value, dict) else {}


def load_earnings_drift() -> dict[str, Any]:
    path = ROOT / "data" / "earnings_drift.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("tickers", {})
    except Exception:
        return {}


def get_earnings_drift(ticker: str) -> dict[str, Any]:
    global _EARNINGS_DRIFT
    if not _EARNINGS_DRIFT:
        _EARNINGS_DRIFT = load_earnings_drift()
    value = _EARNINGS_DRIFT.get(ticker.upper())
    return value if isinstance(value, dict) else {}


def _load_finnhub() -> dict[str, Any]:
    global _FINNHUB_DATA
    if _FINNHUB_DATA is None:
        path = ROOT / "data" / "finnhub_signals.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _FINNHUB_DATA = {
                    "tickers": data.get("tickers", {}),
                    "fda": data.get("fda_calendar", {}),
                    "fda_all": data.get("fda_calendar_all", []),
                }
            except Exception:
                _FINNHUB_DATA = {"tickers": {}, "fda": {}, "fda_all": []}
        else:
            _FINNHUB_DATA = {"tickers": {}, "fda": {}, "fda_all": []}
    return _FINNHUB_DATA


def has_pending_fda_event(ticker: str, within_days: int = 10) -> bool:
    """True only when Finnhub has a ticker-mapped FDA event inside the window."""
    ticker = text(ticker).upper()
    if not ticker:
        return False
    event = _load_finnhub().get("fda", {}).get(ticker)
    if not isinstance(event, dict):
        return False
    event_date = text(event.get("event_date") or event.get("fromDate") or event.get("date"))
    try:
        ed = datetime.strptime(event_date[:10], "%Y-%m-%d")
        days = (ed - datetime.now()).days
        return 0 <= days <= within_days
    except Exception:
        return False


def load_finalist_options() -> dict[str, Any]:
    path = ROOT / "data" / "finalist_options.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("date") == utc_today():
            return data.get("tickers", {})
    except Exception:
        pass
    return {}


def get_finalist_options(ticker: str) -> dict[str, Any]:
    global _FINALIST_OPTIONS
    if not _FINALIST_OPTIONS:
        _FINALIST_OPTIONS = load_finalist_options()
    value = _FINALIST_OPTIONS.get(ticker.upper())
    return value if isinstance(value, dict) else {}

# ═══════════════════════════════════════════════════════════════════════════════
# FINRA DARK POOL LOOKUP (cached)
# ═══════════════════════════════════════════════════════════════════════════════

def load_finra_dark_pool() -> dict[str, dict]:
    path = ROOT / "data" / "finra_dark_pool.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("stocks", {})
    except Exception:
        return {}


_FINRA_DARK_POOL: dict[str, dict] = {}
_INSIDER_TRADES: dict[str, dict] = {}
_CONGRESS_TRADES: dict[str, dict] = {}


def get_finra_dark_pool(ticker: str) -> dict | None:
    global _FINRA_DARK_POOL
    if not _FINRA_DARK_POOL:
        _FINRA_DARK_POOL = load_finra_dark_pool()
    return _FINRA_DARK_POOL.get(ticker.upper())


def load_insider_trades() -> dict[str, dict]:
    path = ROOT / "data" / "insider_trades.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("tickers", {})
    except Exception:
        return {}


def get_insider_trade_data(ticker: str) -> dict | None:
    global _INSIDER_TRADES
    if not _INSIDER_TRADES:
        _INSIDER_TRADES = load_insider_trades()
    return _INSIDER_TRADES.get(ticker.upper())


def load_congress_trades() -> dict[str, dict]:
    path = ROOT / "data" / "congress_trades.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("tickers", {})
    except Exception:
        return {}


def get_congress_trade_data(ticker: str) -> dict | None:
    global _CONGRESS_TRADES
    if not _CONGRESS_TRADES:
        _CONGRESS_TRADES = load_congress_trades()
    return _CONGRESS_TRADES.get(ticker.upper())



def get_market_gex_regime(row: dict[str, Any]) -> tuple[str, str, float | None]:
    """Return market GEX regime, data source, and optional VIX fallback value."""
    row_regime = text(row.get("gex_regime") or row.get("market_gex_regime")).upper()
    if row_regime:
        return row_regime, text(row.get("gex_regime_source") or row.get("regime_source") or "row"), number(row.get("vix_value"))

    data = get_gex_regime_data()
    regime = text(data.get("market_gex_regime")).upper()
    source = text(data.get("regime_source") or "flashalpha")
    vix_value = number(data.get("vix_value"))
    return regime, source, vix_value


# ═══════════════════════════════════════════════════════════════════════════════
# CORE SETUP SCORE (0-100)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_trend_score(row: dict[str, Any]) -> int:
    """0-25 based on price vs 20d MA and EMA stack alignment."""
    price_above_20d = bool(row.get("price_above_20d_ma"))
    above_20d_trend = bool(row.get("above_20d_trend"))
    momentum = row.get("momentum") or {}
    bull_stack = bool(momentum.get("bullStack")) if isinstance(momentum, dict) else False

    if price_above_20d and bull_stack:
        return 25
    if price_above_20d and above_20d_trend:
        return 20
    if price_above_20d:
        return 15
    return 5


def compute_rs_score(row: dict[str, Any]) -> int:
    """0-20 based on RS vs SPY."""
    rs = num(row.get("rsVsSpy"))
    if rs > 2.0:
        return 20
    if rs > 0.0:
        return 12
    return 4


def compute_volume_score(row: dict[str, Any]) -> int:
    """0-20 based on relative volume."""
    # Prefer the 3-bar / 20-bar volumeRatio from Stage 2 scoring
    vratio = num(row.get("volumeRatio"))
    if vratio >= 2.0:
        return 20
    if vratio >= 1.5:
        return 15
    if vratio >= 1.0:
        return 8
    return 2


def compute_setup_quality_score(row: dict[str, Any]) -> int:
    """0-20 based on setupQualityScore."""
    sq = num(row.get("setupQualityScore") or row.get("setup_score"))
    if sq >= 85:
        return 20
    if sq >= 75:
        return 15
    if sq >= 65:
        return 8
    return 3


def compute_rr_score(row: dict[str, Any]) -> int:
    """0-15 based on planned R:R. Uses tp1_adjusted if available."""
    entry = num(row.get("entry") or row.get("price"))
    stop = num(row.get("stopLoss") or row.get("stop"))
    # Use adjusted TP if wall conflict triggered
    target = num(row.get("tp1_adjusted") or row.get("tp1") or row.get("target"))
    if entry <= 0 or stop <= 0 or target <= 0 or entry <= stop:
        return 2
    rr = (target - entry) / (entry - stop)
    if rr >= 3.0:
        return 15
    if rr >= 2.5:
        return 10
    if rr >= 2.0:
        return 6
    return 2


def compute_core_setup_score(row: dict[str, Any]) -> dict[str, Any]:
    """Return core_setup_score (0-100) plus component breakdown."""
    trend = compute_trend_score(row)
    rs = compute_rs_score(row)
    vol = compute_volume_score(row)
    setup = compute_setup_quality_score(row)
    rr = compute_rr_score(row)
    total = trend + rs + vol + setup + rr
    return {
        "core_setup_score": total,
        "core_setup_breakdown": {
            "trend_score": trend,
            "rs_score": rs,
            "volume_score": vol,
            "setup_quality_score": setup,
            "rr_score": rr,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIRMATION SCORE (0-100)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_options_conf(row: dict[str, Any]) -> int:
    """0-25 based on options verdict and data source."""
    ticker = text(row.get("symbol") or row.get("ticker")).upper()
    ticker_options = get_finalist_options(ticker)
    if ticker_options:
        verdict = text(ticker_options.get("options_verdict") or "NEUTRAL").upper()
        row["options_verdict"] = verdict
        row["options_provider_used"] = "barchart_ticker"
        row["put_call_ratio"] = ticker_options.get("put_call_ratio")
        row["iv_rank"] = ticker_options.get("iv_rank")
        row["barchart_ticker_options_signal"] = ticker_options.get("options_signal")
        if verdict == "BULLISH_CONFIRM":
            return 20
        if verdict == "CONFIRM":
            return 12
        if verdict == "NEUTRAL":
            return 0
        if verdict == "CAUTION":
            return -5
        # NO_DATA means this ticker was covered but no usable static ratio was exposed.

    verdict = text(row.get("options_verdict")).upper()
    provider = text(row.get("options_provider_used")).lower()
    v2 = text(row.get("options_verdict_v2")).upper()

    # Prefer v2 verdict if available
    if v2:
        if v2 == "STRONG_BULLISH_CONFIRM" and provider == "impliedoptions_auth":
            return 25
        if v2 == "BULLISH_CONFIRM" and provider == "impliedoptions_auth":
            return 20
        if v2 == "STRONG_BULLISH_CONFIRM":
            return 22
        if v2 == "BULLISH_CONFIRM":
            return 18
        if v2 == "NEUTRAL":
            return 5
        if v2 in {"CAUTION", "BEARISH_WARNING"}:
            return 0
        return 2

    # Legacy verdict
    if verdict == "CONFIRM" and provider == "impliedoptions_auth":
        return 20
    if verdict == "CONFIRM":
        return 14
    if verdict == "NEUTRAL":
        return 4
    if verdict == "CAUTION":
        return 0
    return 2


def compute_options_structure_conf(row: dict[str, Any]) -> int:
    """
    +5 / -4 bonus/penalty from options structure signals.

    Bonuses:
      +3 stop_wall_support = true
      +2 max_pain_signal = BELOW_PAIN
      +2 tp1 clear path (call_wall_1 > tp1 by > 5%)
      +1 price_vs_gamma_flip = ABOVE_FLIP

    Penalties:
      -2 tp1_wall_conflict = true
      -2 max_pain_signal = ABOVE_PAIN AND days_to_weekly_expiry <= 5
      -1 price_vs_gamma_flip = BELOW_FLIP
    """
    score = 0
    bonuses: list[str] = []
    penalties: list[str] = []

    # Bonuses
    if row.get("stop_wall_support") is True:
        score += 3
        bonuses.append("put_wall_protects_stop")

    if text(row.get("max_pain_signal")).upper() == "BELOW_PAIN":
        score += 2
        bonuses.append("below_max_pain_gravity_up")

    # Clear path to TP1: no call wall within 5% below TP1
    tp1 = number(row.get("tp1") or row.get("target"))
    call_w1 = number(row.get("call_wall_1"))
    tp_conflict = row.get("tp1_wall_conflict")
    if tp1 is not None and tp1 > 0 and not tp_conflict:
        if call_w1 is not None and call_w1 > 0:
            gap = (call_w1 - tp1) / tp1 * 100
            if gap > 5:
                score += 2
                bonuses.append("clear_path_to_tp1")
        elif call_w1 is None:
            # No call wall at all = clear path
            score += 2
            bonuses.append("clear_path_to_tp1")

    if text(row.get("price_vs_gamma_flip")).upper() == "ABOVE_FLIP":
        score += 1
        bonuses.append("positive_gamma_environment")

    # Penalties
    if tp_conflict is True:
        score -= 2
        penalties.append("tp1_blocked_by_call_wall")

    if text(row.get("max_pain_signal")).upper() == "ABOVE_PAIN":
        days = number(row.get("days_to_weekly_expiry"))
        if days is not None and days <= 5:
            score -= 2
            penalties.append("expiry_gravity_against")

    if text(row.get("price_vs_gamma_flip")).upper() == "BELOW_FLIP":
        score -= 1
        penalties.append("negative_gamma_volatile")

    # Cap
    score = max(-4, min(5, score))
    return score


def compute_chronos_conf(row: dict[str, Any]) -> int:
    """0-20 based on forecast direction and band tightness."""
    combined_dir = text(row.get("combined_forecast_dir")).upper()
    band = num(row.get("combined_band_pct"))
    chronos_dir = text(row.get("chronos_dir")).upper()

    # Use combined forecast if available, else chronos alone
    direction = combined_dir or chronos_dir

    if direction == "STRONG_UP":
        return 20 if band is not None and band < 3 else 18
    if direction == "UP":
        if band is not None and band < 3:
            return 18
        if band is not None and band < 5:
            return 12
        return 8
    if direction in {"LEAN_UP", "FLAT"}:
        return 5
    if direction in {"LEAN_DOWN", "DOWN"}:
        return 2
    if direction == "STRONG_DOWN":
        if band is not None and band < 3:
            return 0
        if band is not None and band < 5:
            return 5
        return 10
    return 5


def compute_gex_structural_conf(row: dict[str, Any]) -> int:
    """0-12 structural regime score from FlashAlpha or VIX fallback GEX regime."""
    gex_regime, source, vix_value = get_market_gex_regime(row)
    spy_rs = num(row.get("spy_rs_5d") or row.get("rsVsSpy"))

    if gex_regime in {"ABOVE_FLIP", "BULLISH", "POSITIVE", "POSITIVE_GAMMA"}:
        return 12 if spy_rs > 0 else 9
    if gex_regime in {"NEUTRAL", "DAMPENING"}:
        return 6
    if gex_regime in {"BELOW_FLIP", "CHOPPY"}:
        return 3
    if gex_regime in {"NEGATIVE_GAMMA", "BEARISH", "NEGATIVE", "AMPLIFYING"}:
        return 0

    # If VIX fallback exists but maps to UNKNOWN, keep a small neutral floor only
    # when the data source is known. Missing data gets no structural credit.
    if source == "vix_fallback" and vix_value is not None:
        return 3
    return 0


def compute_seasonal_conf(row: dict[str, Any]) -> int:
    """0-15 based on seasonality signal."""
    sig = text(row.get("seasonal_signal")).upper()
    if sig == "TAILWIND":
        return 15
    if sig == "NEUTRAL":
        return 5
    if sig == "HEADWIND":
        return 0
    return 5


def compute_dark_pool_conf(row: dict[str, Any]) -> int:
    """0-15 based on FINRA RegSHO short-volume dark pool proxy.

    Prefer real FINRA daily short-volume data when available; fall back to
    legacy row-level dark_pool_signal if FINRA file is missing/stale.
    """
    ticker = text(row.get("symbol") or row.get("ticker")).upper()
    finra = get_finra_dark_pool(ticker) if ticker else None
    if finra:
        sig = text(finra.get("dark_pool_signal") or "NORMAL").upper()
        row["dark_pool_signal"] = sig
        row["dark_pool_pct"] = finra.get("dark_pool_pct")
        row["dark_pool_source"] = "finra_ats"
    else:
        sig = text(row.get("dark_pool_signal")).upper()
        row["dark_pool_source"] = row.get("dark_pool_source") or "proxy"
    if sig == "STRONG":
        return 15
    if sig == "MODERATE":
        return 10 if finra else 8
    if sig == "WEAK":
        return 5 if finra else 3
    return 0


def compute_x_signal_conf(row: dict[str, Any]) -> int:
    """0-15 based on X/FinTwit signal strength."""
    post_count = int(num(row.get("post_count")))
    sentiment = num(row.get("sentiment_score"))
    sources = row.get("all_sources") or []
    if isinstance(sources, str):
        sources = [sources]
    has_x = "x" in {text(s).lower() for s in sources}

    if not has_x:
        return 0
    if post_count >= 5 and sentiment > 70:
        return 15
    if post_count >= 5:
        return 12
    if post_count >= 3:
        return 8
    if post_count >= 2:
        return 4
    return 0


def compute_analyst_conf(row: dict[str, Any]) -> int:
    """0-10 based on analyst changes."""
    ac = row.get("analyst_change")
    summary = text(ac.get("summary") if isinstance(ac, dict) else ac).lower()
    if "upgrade" in summary:
        return 10
    if "raised" in summary or "increase" in summary:
        return 5
    if "downgrade" in summary or "cut" in summary:
        return 0
    return 3


def compute_danelfin_conf(row: dict[str, Any]) -> int:
    """0-8 bonus from Danelfin AI score (if available)."""
    ticker = text(row.get("symbol") or row.get("ticker")).upper()
    ds = get_danelfin_score(ticker)
    if not ds:
        return 0
    ai_score = num(ds.get("ai_score"))
    if ai_score >= 9:
        return 8
    if ai_score >= 7:
        return 5
    if ai_score >= 5:
        return 2
    return 0



def compute_insider_conf(row: dict[str, Any]) -> int:
    """0-15 based on insider buying signal."""
    ticker = text(row.get("symbol") or row.get("ticker")).upper()
    insider = get_insider_trade_data(ticker) if ticker else None
    if insider:
        signal = text(insider.get("insider_buy_signal") or "NONE").upper()
        value = num(insider.get("insider_buy_value"))
        count = int(num(insider.get("insider_buy_count")))
        is_cluster = bool(insider.get("is_cluster"))
        row["insider_buy_signal"] = signal
        row["insider_buy_value"] = value
        row["insider_buy_count"] = count
        row["is_cluster_buy"] = is_cluster
        row["insider_data_source"] = "openinsider"
        if is_cluster:
            return 15
        if signal == "STRONG":
            return 12
        if signal == "MODERATE":
            return 8
        if signal == "WEAK":
            return 4
        return 0

    signal = text(row.get("insider_buy_signal", "")).upper()
    value = num(row.get("insider_buy_value"))
    count = int(num(row.get("insider_buy_count")))
    row["insider_data_source"] = row.get("insider_data_source") or "none"

    if signal == "STRONG" or value >= 500000:
        return 15
    elif signal == "MODERATE" or value >= 100000:
        return 8
    elif count >= 1 and value > 0:
        return 4
    return 0


def compute_congress_conf(row: dict[str, Any]) -> int:
    """0-12 based on congressional purchase disclosures."""
    ticker = text(row.get("symbol") or row.get("ticker")).upper()
    congress = get_congress_trade_data(ticker) if ticker else None
    if not congress:
        row.setdefault("congress_signal", "NONE")
        return 0
    signal = text(congress.get("congress_signal") or "NONE").upper()
    is_cluster = bool(congress.get("is_cluster"))
    is_bipartisan = bool(congress.get("is_bipartisan"))
    politicians = congress.get("politician_names")
    if not politicians:
        politicians = [
            p.get("name")
            for p in congress.get("politicians", [])
            if isinstance(p, dict) and p.get("name")
        ]
    row["congress_signal"] = signal
    row["congress_buy_count"] = congress.get("congress_buy_count", 0)
    row["congress_politicians"] = politicians[:2] if isinstance(politicians, list) else []
    row["congress_is_cluster"] = is_cluster
    row["congress_is_bipartisan"] = is_bipartisan
    if signal == "STRONG":
        return 12
    if signal == "MODERATE":
        return 7
    if signal == "WEAK":
        return 3
    return 0



def compute_squeeze_conf(row: dict[str, Any]) -> int:
    """0-10 based on short squeeze potential."""
    score = num(row.get("short_squeeze_score"))
    short_pct = num(row.get("short_percent_float"))

    if score >= 70 or short_pct >= 20:
        return 10
    elif score >= 50 or short_pct >= 15:
        return 6
    elif score >= 30 or short_pct >= 10:
        return 3
    return 0



def compute_news_catalyst_conf(row: dict[str, Any]) -> int:
    """-15 to +10 from today's external news catalyst file."""
    ticker = text(row.get("symbol") or row.get("ticker")).upper()
    news = get_news_catalyst(ticker)
    if not news:
        return 0

    verdict = text(news.get("news_verdict") or "NO_DATA").upper()
    score = num(news.get("news_sentiment_score"))
    row["news_verdict"] = verdict
    row["news_sentiment_score"] = score
    row["news_headline"] = news.get("best_headline")
    row["news_article_count"] = news.get("article_count")
    row["news_sources_used"] = news.get("sources_used")

    if verdict == "POSITIVE_CATALYST":
        return 10
    if verdict == "MILD_POSITIVE":
        return 5
    if verdict == "CAUTION":
        return -5
    if verdict == "DANGER":
        return -15
    return 0


def compute_recommendation_conf(row: dict[str, Any]) -> int:
    """Finnhub analyst recommendation trend contribution for Family C."""
    ticker = text(row.get("symbol") or row.get("ticker")).upper()
    data = _load_finnhub().get("tickers", {}).get(ticker, {})
    rec = data.get("recommendation") if isinstance(data, dict) else None
    if not isinstance(rec, dict) or not rec:
        row["analyst_rec_direction"] = None
        row["analyst_bull_pct"] = None
        return 0

    direction = text(rec.get("direction")).upper()
    bull_pct = num(rec.get("bull_pct"))
    row["analyst_rec_direction"] = direction or None
    row["analyst_bull_pct"] = bull_pct

    if direction == "UPGRADING":
        return 8
    if direction == "STABLE" and num(rec.get("net_score")) > 3:
        return 4
    if direction == "DOWNGRADING":
        return -4
    return 0


def compute_pead_conf(row: dict[str, Any]) -> int:
    """Post-earnings drift and estimate revision confirmation."""
    ticker = text(row.get("symbol") or row.get("ticker")).upper()
    data = get_earnings_drift(ticker)
    finnhub_earn = (_load_finnhub().get("tickers", {}).get(ticker, {}) or {}).get("earnings") or {}

    score = 0
    signals: list[str] = []
    surprise = data.get("earnings_surprise") or data.get("surprise") or {}
    revision = data.get("estimate_revision") or data.get("revision") or {}
    if not surprise and isinstance(finnhub_earn, dict) and finnhub_earn:
        surprise = {
            "actual": finnhub_earn.get("actual"),
            "estimate": finnhub_earn.get("estimate"),
            "surprise_pct": finnhub_earn.get("surprise_pct"),
            "period": finnhub_earn.get("period"),
            "days_since_earnings": finnhub_earn.get("days_since"),
            "is_beat": finnhub_earn.get("is_beat"),
            "source": "finnhub",
        }

    surprise_pct = num(surprise.get("surprise_pct"))
    days_since = surprise.get("days_since_earnings")
    try:
        days_since_int = int(days_since) if days_since is not None else None
    except Exception:
        days_since_int = None
    is_beat = surprise.get("is_beat")

    row["earnings_surprise_pct"] = surprise_pct if surprise else None
    row["days_since_earnings"] = days_since_int
    row["earnings_surprise_is_beat"] = is_beat
    row["earnings_surprise_source"] = surprise.get("source") if surprise else None

    if days_since_int is not None and days_since_int <= 15:
        if is_beat is True and surprise_pct >= 5:
            score += 8
            signals.append(f"earnings beat {surprise_pct:.1f}% {days_since_int}d ago")
        elif is_beat is False and surprise_pct <= -5:
            score -= 8
            signals.append(f"earnings miss {surprise_pct:.1f}% {days_since_int}d ago")

    direction = text(revision.get("direction")).upper()
    revision_pct = num(revision.get("revision_pct"))
    row["estimate_revision"] = direction or None
    if direction == "RISING" and revision_pct > 2:
        score += 5
        signals.append(f"estimates rising {revision_pct:.1f}%")
    elif direction == "FALLING" and revision_pct < -2:
        score -= 5
        signals.append(f"estimates falling {revision_pct:.1f}%")

    market_cap = num(row.get("market_cap") or row.get("marketCap"))
    if market_cap > 500_000_000_000:
        score = int(score * 0.5)
        if signals and score:
            signals.append("mega-cap dampened")

    score = max(-12, min(12, score))
    row["pead_score"] = score
    row["pead_signals"] = signals
    return score


def compute_tape_conf(row: dict[str, Any]) -> int:
    """0-10 bullish tape reward (bearish handled by risk_score)."""
    signal = text(row.get("tape_signal")).upper()
    if signal == "BULLISH":
        return 10
    return 0


def compute_confirmation_score(row: dict[str, Any], market_regime: str | None = None) -> dict[str, Any]:
    """Return confirmation_score (0-100) via 4-family orthogonal grouping.

    Regime-adaptive: family caps adjust based on current market regime.
    """
    options = compute_options_conf(row)
    chronos = compute_chronos_conf(row)
    seasonal = compute_seasonal_conf(row)
    dark_pool = compute_dark_pool_conf(row)
    x_signal = compute_x_signal_conf(row)
    analyst = compute_analyst_conf(row)
    danelfin = compute_danelfin_conf(row)
    options_structure = compute_options_structure_conf(row)
    insider = compute_insider_conf(row)
    squeeze = compute_squeeze_conf(row)
    news_catalyst = compute_news_catalyst_conf(row)
    congress = compute_congress_conf(row)
    recommendation = compute_recommendation_conf(row)
    pead = compute_pead_conf(row)
    tape = compute_tape_conf(row)
    gex_structural = compute_gex_structural_conf(row)
    gex_regime, gex_source, gex_vix_value = get_market_gex_regime(row)

    # ── Regime detection & adaptive caps ────────────────────────────────
    regime = text(market_regime).upper() if market_regime else detect_market_regime(row)
    caps = get_regime_caps(regime)

    # ── Signal family grouping ──────────────────────────────────────────
    # Family A — MOMENTUM (price/volume action today)
    family_a_raw = sum(filter(None, [x_signal]))
    family_a = min(int(caps["momentum"]), family_a_raw)

    # Family B — POSITIONING/FLOW (where money is)
    family_b_raw = sum(filter(None, [options, options_structure, dark_pool, insider, tape]))
    family_b = min(int(caps["positioning"]), family_b_raw)

    # Family C — FUNDAMENTAL/CATALYST (why it moves)
    family_c_raw = sum(filter(None, [seasonal, analyst, danelfin, squeeze, news_catalyst, congress, pead, recommendation]))
    family_c = min(int(caps["catalyst"]), family_c_raw)

    # Family D — STRUCTURAL/REGIME (market context)
    family_d_raw = sum(filter(None, [chronos, gex_structural]))
    family_d = min(int(caps["structural"]), family_d_raw)

    # Total confirmation = sum of capped families (max 100)
    total = family_a + family_b + family_c + family_d
    total = min(100, total)

    # Count how many families have meaningful signal (>= 5)
    families_firing = sum([
        1 if family_a >= 5 else 0,
        1 if family_b >= 5 else 0,
        1 if family_c >= 5 else 0,
        1 if family_d >= 5 else 0,
    ])

    return {
        "confirmation_score": total,
        "confirmation_breakdown": {
            "options_conf": options,
            "chronos_conf": chronos,
            "seasonal_conf": seasonal,
            "dark_pool_conf": dark_pool,
            "x_signal_conf": x_signal,
            "analyst_conf": analyst,
            "options_structure_conf": options_structure,
            "danelfin_conf": danelfin,
            "insider_conf": insider,
            "squeeze_conf": squeeze,
            "news_catalyst_conf": news_catalyst,
            "congress_conf": congress,
            "recommendation_conf": recommendation,
            "pead_conf": pead,
            "tape_conf": tape,
            "gex_structural_conf": gex_structural,
            "gex_regime": gex_regime,
            "gex_regime_source": gex_source,
            "gex_vix_value": gex_vix_value,
        },
        "family_scores": {
            "momentum": family_a,
            "positioning": family_b,
            "catalyst": family_c,
            "structural": family_d,
            "momentum_raw": family_a_raw,
            "positioning_raw": family_b_raw,
            "catalyst_raw": family_c_raw,
            "structural_raw": family_d_raw,
            "families_firing": families_firing,
        },
        "market_regime_detected": regime,
        "regime_caps_applied": {k: v for k, v in caps.items() if k in ("momentum", "positioning", "catalyst", "structural")},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RISK SCORE (0-100, higher = more risk)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_earnings_risk(row: dict[str, Any]) -> int:
    """0-30 based on proximity to earnings. Healthcare gets extra FDA gate penalty."""
    days = number(row.get("earnings_in_days") or row.get("days_to_earnings"))
    if days is None:
        return 0
    base = 0
    if days < 8:
        base = 30
    elif days <= 14:
        base = 15
    # Healthcare FDA gate: stricter screening for biotech/pharma near earnings
    sector = text(row.get("sector")).lower()
    if "health" in sector or "biotech" in sector or "pharma" in sector:
        if days is not None and days <= 14:
            base = min(30, base + 10)
    return base


def compute_forecast_risk(row: dict[str, Any]) -> int:
    """0-25 based on forecast direction and disagreement."""
    combined_dir = text(row.get("combined_forecast_dir")).upper()
    decision = text(row.get("forecastDecision")).upper()
    agree = bool(row.get("models_agree"))

    score = 0
    if combined_dir == "STRONG_DOWN":
        score += 20
    elif combined_dir == "DOWN":
        score += 15
    elif combined_dir in {"CONFLICTED", "LEAN_DOWN"}:
        score += 10
    elif combined_dir == "FLAT":
        score += 5

    if decision == "REJECT":
        score += 8
    if not agree:
        score += 5

    return min(score, 25)


def compute_regime_risk(row: dict[str, Any], market_regime: str | None = None) -> int:
    """0-20 based on VIX regime."""
    regime = text(market_regime or row.get("market_regime") or row.get("regime")).upper()
    if regime == "RISK_OFF":
        return 20
    if regime == "CAUTION":
        return 12
    return 0


def compute_extension_risk(row: dict[str, Any]) -> int:
    """0-15 based on price extension and volatility."""
    score = 0
    atr_pct = num(row.get("atrPct"))
    above_trend = bool(row.get("above_20d_trend"))

    if atr_pct > 6.0:
        score += 8
    if not above_trend:
        score += 8

    # Distance from 52-week high if available.
    dist_52w = number(row.get("pct_from_52wk_high") or row.get("distance_from_52w_high_pct"))
    if dist_52w is not None and dist_52w > -5:
        score += 5

    return min(score, 15)


def compute_options_risk(row: dict[str, Any]) -> int:
    """0-10 based on options verdict and data quality."""
    verdict = text(row.get("options_verdict")).upper()
    v2 = text(row.get("options_verdict_v2")).upper()
    quality = text(row.get("options_data_quality")).lower()

    if v2 in {"CAUTION", "BEARISH_WARNING"}:
        return 10
    if verdict == "CAUTION":
        return 10
    if verdict == "NO_DATA":
        return 5
    if quality in {"poor", "stale", "fallback"}:
        return 3
    return 0


def compute_tape_risk(row: dict[str, Any]) -> int:
    """0-10 based on tape signal (if available)."""
    tape = text(row.get("tape_signal")).upper()
    if tape == "BEARISH":
        return 10
    if tape == "NEUTRAL":
        return 3
    return 0


def compute_council_risk(row: dict[str, Any]) -> int:
    """0-10 based on council SKIP/FORCE_SKIP from any model."""
    models = ["claude", "kimi", "gemini", "gpt4o"]
    for mk in models:
        v = text(row.get(f"{mk}_verdict")).upper()
        if v in {"SKIP", "FORCE_SKIP"}:
            return 8
    # ABSTAIN from all models = no opinion, not a risk
    return 0


def compute_risk_score(row: dict[str, Any], market_regime: str | None = None) -> dict[str, Any]:
    """Return risk_score (0-100) plus component breakdown."""
    earnings = compute_earnings_risk(row)
    forecast = compute_forecast_risk(row)
    regime = compute_regime_risk(row, market_regime)
    extension = compute_extension_risk(row)
    options = compute_options_risk(row)
    tape = compute_tape_risk(row)
    council = compute_council_risk(row)

    total = earnings + forecast + regime + extension + options + tape + council
    return {
        "risk_score": total,
        "risk_breakdown": {
            "earnings_risk": earnings,
            "forecast_risk": forecast,
            "regime_risk": regime,
            "extension_risk": extension,
            "options_risk": options,
            "tape_risk": tape,
            "council_risk": council,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE QUALITY COMPOSITE
# ═══════════════════════════════════════════════════════════════════════════════

def _band(value: float, good: float, okay: float) -> str:
    if value >= good:
        return "strong"
    if value >= okay:
        return "mixed"
    return "weak"


def build_evidence_scoreboard(
    row: dict[str, Any],
    core: dict[str, Any],
    confirmation: dict[str, Any],
    risk: dict[str, Any],
    dq: dict[str, Any],
) -> dict[str, Any]:
    """Summarize the trading evidence in desk-readable families."""
    families = confirmation.get("family_scores", {})
    breakdown = confirmation.get("confirmation_breakdown", {})
    risk_score = risk.get("risk_score", 0)
    data_quality = dq.get("data_quality_score", 0)

    return {
        "technical": {
            "status": _band(core.get("core_setup_score", 0), 75, 55),
            "score": core.get("core_setup_score", 0),
            "drivers": core.get("core_setup_breakdown", {}),
        },
        "flow_positioning": {
            "status": _band(families.get("positioning", 0), 18, 8),
            "score": families.get("positioning", 0),
            "drivers": {
                "options": breakdown.get("options_conf", 0),
                "options_structure": breakdown.get("options_structure_conf", 0),
                "dark_pool": breakdown.get("dark_pool_conf", 0),
                "insider": breakdown.get("insider_conf", 0),
                "tape": breakdown.get("tape_conf", 0),
            },
        },
        "catalyst": {
            "status": _band(families.get("catalyst", 0), 12, 5),
            "score": families.get("catalyst", 0),
            "drivers": {
                "seasonality": breakdown.get("seasonal_conf", 0),
                "analyst": breakdown.get("analyst_conf", 0),
                "danelfin": breakdown.get("danelfin_conf", 0),
                "squeeze": breakdown.get("squeeze_conf", 0),
                "news": breakdown.get("news_catalyst_conf", 0),
                "congress": breakdown.get("congress_conf", 0),
                "pead": breakdown.get("pead_conf", 0),
                "recommendation": breakdown.get("recommendation_conf", 0),
            },
        },
        "regime_structure": {
            "status": _band(families.get("structural", 0), 12, 5),
            "score": families.get("structural", 0),
            "drivers": {
                "chronos": breakdown.get("chronos_conf", 0),
                "gex": breakdown.get("gex_structural_conf", 0),
                "gex_regime": breakdown.get("gex_regime"),
                "gex_source": breakdown.get("gex_regime_source"),
                "vix": breakdown.get("gex_vix_value"),
            },
        },
        "risk": {
            "status": "clean" if risk_score < 25 else "elevated" if risk_score < 50 else "high",
            "score": risk_score,
            "drivers": risk.get("risk_breakdown", {}),
        },
        "data_quality": {
            "status": dq.get("data_quality_label"),
            "score": data_quality,
            "checks": dq.get("data_quality_checks", {}),
        },
        "families_firing": families.get("families_firing", 0),
    }


def build_trade_readiness_tier(
    trade_quality: int,
    core: dict[str, Any],
    confirmation: dict[str, Any],
    risk: dict[str, Any],
    dq: dict[str, Any],
) -> dict[str, Any]:
    """Assign desk-style readiness tier with reasons."""
    families = confirmation.get("family_scores", {})
    families_firing = int(families.get("families_firing") or 0)
    risk_score = risk.get("risk_score", 0)
    data_quality = dq.get("data_quality_score", 0)
    core_score = core.get("core_setup_score", 0)
    conf_score = confirmation.get("confirmation_score", 0)

    reasons: list[str] = []
    blockers: list[str] = []
    if core_score >= 75:
        reasons.append("strong technical setup")
    if conf_score >= 55:
        reasons.append("multiple confirmation families")
    if families_firing >= 3:
        reasons.append(f"{families_firing} evidence families firing")
    if risk_score < 25:
        reasons.append("risk stack is clean")
    if data_quality >= 70:
        reasons.append("data quality is good")

    if risk_score >= 50:
        blockers.append("risk score too high")
    if data_quality < 40:
        blockers.append("data quality too low")
    if families_firing < 2:
        blockers.append("not enough independent evidence")
    if core_score < 45:
        blockers.append("technical setup is weak")

    if blockers:
        tier = "D_PASS" if trade_quality < 45 or risk_score >= 50 else "C_NEEDS_CONFIRMATION"
    elif trade_quality >= 75 and families_firing >= 3 and risk_score < 30 and data_quality >= 60:
        tier = "A_PLUS_TRADE"
    elif trade_quality >= 65 and families_firing >= 3 and risk_score < 40:
        tier = "A_TRADE"
    elif trade_quality >= 55:
        tier = "B_WATCH"
    elif trade_quality >= 45:
        tier = "C_NEEDS_CONFIRMATION"
    else:
        tier = "D_PASS"

    return {
        "tier": tier,
        "reasons": reasons,
        "blockers": blockers,
        "action": {
            "A_PLUS_TRADE": "actionable if market-open trigger confirms",
            "A_TRADE": "actionable with confirmation and disciplined sizing",
            "B_WATCH": "watchlist only; wait for live trigger",
            "C_NEEDS_CONFIRMATION": "do not enter without fresh confirmation",
            "D_PASS": "pass unless new information changes the thesis",
        }.get(tier, "watch"),
    }


def build_trader_thesis_card(
    row: dict[str, Any],
    trade_quality: int,
    readiness: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Create a concise trader/analyst thesis card for the idea."""
    ticker = text(row.get("symbol") or row.get("ticker")).upper()
    setup = text(row.get("setup") or row.get("setupType") or "technical setup")
    price = number(row.get("price"))
    entry_zone = row.get("entryZone")
    entry = None
    if isinstance(entry_zone, list) and len(entry_zone) >= 2:
        entry = f"{entry_zone[0]}-{entry_zone[-1]}"
    elif price is not None:
        entry = round(price, 4)

    stop = row.get("stopLoss") or row.get("stop")
    target = row.get("tp1_adjusted") or row.get("tp1") or row.get("target")
    gex = evidence.get("regime_structure", {}).get("drivers", {}).get("gex_regime")
    options_verdict = text(row.get("options_verdict") or row.get("options_verdict_v2") or "not confirmed")
    forecast = text(row.get("combined_forecast_dir") or row.get("chronos_dir") or "not available")
    catalyst = text(row.get("catalyst_summary") or row.get("analyst_signal") or row.get("seasonal_signal") or "no single catalyst")

    invalidation = []
    if stop:
        invalidation.append(f"break below stop {stop}")
    if text(row.get("options_verdict")).upper() == "CAUTION":
        invalidation.append("options flow turns cautionary")
    if text(row.get("combined_forecast_dir")).upper() in {"DOWN", "STRONG_DOWN"}:
        invalidation.append("forecast points down")
    if not invalidation:
        invalidation.append("fails VWMA/opening-range confirmation")

    return {
        "ticker": ticker,
        "tier": readiness.get("tier"),
        "trade_quality_score": trade_quality,
        "why_now": f"{setup} with {evidence.get('technical', {}).get('status')} technical evidence and {options_verdict} options context.",
        "primary_catalyst": catalyst,
        "confirmation": [
            f"forecast: {forecast}",
            f"regime/GEX: {gex or 'not available'}",
            f"families firing: {evidence.get('families_firing', 0)}",
        ],
        "entry_trigger": "market-open VWMA/relative-volume confirmation required",
        "entry_zone": entry,
        "stop": stop,
        "target_1": target,
        "invalidation": invalidation,
        "do_not_trade_if": readiness.get("blockers") or ["market regime worsens or live confirmation fails"],
        "analyst_note": readiness.get("action"),
    }


def compute_trade_quality(row: dict[str, Any], market_regime: str | None = None) -> dict[str, Any]:
    """Compute full 3-category scoring for a single idea row.

    Regime-adaptive: weights adjust based on current market regime.
    """
    # Detect regime (use explicit param, or detect from row, or env)
    regime = text(market_regime).upper() if market_regime else detect_market_regime(row)
    caps = get_regime_caps(regime)

    core = compute_core_setup_score(row)
    confirmation = compute_confirmation_score(row, regime)
    risk = compute_risk_score(row, regime)
    dq = compute_data_quality_score(row)

    # Data quality cap: if data quality < 30, cap core setup at B-tier max (68)
    core_setup = core["core_setup_score"]
    data_quality_cap_applied = False
    if dq["data_quality_score"] < 30:
        if core_setup > 68:
            core_setup = 68
            data_quality_cap_applied = True

    trade_quality = round(
        core_setup * caps["core_weight"]
        + confirmation["confirmation_score"] * caps["confirmation_weight"]
        - risk["risk_score"] * caps["risk_weight"]
    )
    # Clamp to 0-100
    trade_quality = max(0, min(100, trade_quality))

    label = "WEAK"
    if trade_quality >= 75:
        label = "HIGH CONVICTION"
    elif trade_quality >= 65:
        label = "GOOD SETUP"
    elif trade_quality >= 55:
        label = "MODERATE"
    elif trade_quality >= 45:
        label = "WATCHLIST ONLY"

    evidence = build_evidence_scoreboard(row, core, confirmation, risk, dq)
    readiness = build_trade_readiness_tier(trade_quality, core, confirmation, risk, dq)
    thesis = build_trader_thesis_card(row, trade_quality, readiness, evidence)

    result = {
        **core,
        **confirmation,
        **risk,
        **dq,
        "core_setup_score": core_setup,
        "trade_quality_score": trade_quality,
        "trade_quality_label": label,
        "trade_quality_finalist": trade_quality >= 55,  # advisory only; actual cutoff in confluence_scoring.py
        "trade_readiness_tier": readiness["tier"],
        "trade_readiness": readiness,
        "evidence_scoreboard": evidence,
        "trader_thesis_card": thesis,
        "news_verdict": row.get("news_verdict"),
        "news_sentiment_score": row.get("news_sentiment_score"),
        "news_headline": row.get("news_headline"),
        "news_article_count": row.get("news_article_count"),
        "news_sources_used": row.get("news_sources_used"),
        "pead_score": row.get("pead_score"),
        "pead_signals": row.get("pead_signals"),
        "earnings_surprise_pct": row.get("earnings_surprise_pct"),
        "days_since_earnings": row.get("days_since_earnings"),
        "earnings_surprise_is_beat": row.get("earnings_surprise_is_beat"),
        "earnings_surprise_source": row.get("earnings_surprise_source"),
        "estimate_revision": row.get("estimate_revision"),
        "analyst_rec_direction": row.get("analyst_rec_direction"),
        "analyst_bull_pct": row.get("analyst_bull_pct"),
        "fda_event_flag": row.get("fda_event_flag", False),
        "options_provider_used": row.get("options_provider_used"),
        "put_call_ratio": row.get("put_call_ratio"),
        "iv_rank": row.get("iv_rank"),
        "barchart_ticker_options_signal": row.get("barchart_ticker_options_signal"),
        "market_regime": regime,
        "regime_weights_applied": {
            "core": caps["core_weight"],
            "confirmation": caps["confirmation_weight"],
            "risk": caps["risk_weight"],
        },
        "data_quality_cap_applied": data_quality_cap_applied,
    }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# DATA QUALITY SCORE (0-100)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_data_quality_score(row: dict[str, Any]) -> dict[str, Any]:
    """Score data source freshness and availability."""
    score = 0
    checks: dict[str, Any] = {}

    # FMP data freshness (assume fresh if quote data exists)
    has_quote = bool(row.get("price") and row.get("volumeRatio"))
    checks["fmp_quote_available"] = has_quote
    if has_quote:
        score += 12

    # Options data quality
    provider = text(row.get("options_provider_used")).lower()
    if provider in {"impliedoptions_auth", "barchart_ticker"}:
        score += 15
        checks["options_source"] = "primary"
    elif provider in {"barchart_uoa", "yahoo", "alpha_vantage"}:
        score += 5
        checks["options_source"] = "fallback"
    else:
        checks["options_source"] = "none"

    # Earnings date confirmed
    has_earnings = bool(row.get("earnings_in_days") is not None)
    checks["earnings_known"] = has_earnings
    if has_earnings:
        score += 8

    # Forecast data
    has_forecast = bool(row.get("chronos_dir") or row.get("combined_forecast_dir"))
    checks["forecast_available"] = has_forecast
    if has_forecast:
        score += 12

    # Pre-market check
    has_premarket = bool(row.get("pre_market_checked_at"))
    checks["pre_market_checked"] = has_premarket
    if has_premarket:
        score += 8

    # Tape signal
    has_tape = bool(row.get("tape_signal"))
    checks["tape_available"] = has_tape
    if has_tape:
        score += 8

    # Seasonality
    has_seasonal = text(row.get("seasonal_signal")).upper() not in {"", "INSUFFICIENT_DATA"}
    checks["seasonality_available"] = has_seasonal
    if has_seasonal:
        score += 8

    # Dark pool
    finra_dp = get_finra_dark_pool(text(row.get("symbol") or row.get("ticker")).upper())
    has_dp = (
        text(finra_dp.get("dark_pool_signal") if finra_dp else row.get("dark_pool_signal")).upper()
        not in {"", "INSUFFICIENT_DATA"}
    )
    checks["dark_pool_available"] = has_dp
    if has_dp:
        score += 8

    # No scraper warnings (track_a_fail is a health check)
    track_fail = bool(row.get("track_a_fail"))
    checks["track_a_healthy"] = not track_fail
    if not track_fail:
        score += 8

    # Insider data (from catalyst, not always available)
    has_insider = bool(row.get("insider_buy_count") or row.get("insider_buy_value"))
    checks["insider_available"] = has_insider
    if has_insider:
        score += 8

    score = min(score, 100)

    label = "POOR"
    if score >= 85:
        label = "EXCELLENT"
    elif score >= 70:
        label = "GOOD"
    elif score >= 50:
        label = "FAIR"

    return {
        "data_quality_score": score,
        "data_quality_label": label,
        "data_quality_checks": checks,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# BEAR CASE GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

def generate_bear_case_points(row: dict[str, Any]) -> list[dict[str, str]]:
    """Generate a list of bear-case warning strings for display."""
    points: list[dict[str, str]] = []

    def add(severity: str, text_str: str) -> None:
        points.append({"severity": severity, "text": text_str})

    # Council SKIP/FORCE_SKIP
    for mk, name in [("claude", "Claude"), ("kimi", "Kimi"), ("gemini", "Gemini"), ("gpt4o", "GPT-4o")]:
        v = text(row.get(f"{mk}_verdict")).upper()
        reason = text(row.get(f"{mk}_reasoning"))
        if v in {"SKIP", "FORCE_SKIP"}:
            add("high", f"Council: {name} flagged {v} — {reason[:80]}")

    # Gemini reasoning always included if available
    gemini_reason = text(row.get("gemini_reasoning") or row.get("gemini_r2_reasoning"))
    if gemini_reason and not any(p["text"].startswith("Council: Gemini") for p in points):
        add("medium", f"Short seller view: {gemini_reason[:120]}")

    # Forecast risk
    combined_dir = text(row.get("combined_forecast_dir")).upper()
    if combined_dir in {"DOWN", "STRONG_DOWN"}:
        add("high", f"AI forecast predicts downward price movement ({combined_dir})")
    elif combined_dir == "CONFLICTED":
        add("medium", "Forecast models disagree on direction")

    # Options caution
    v2 = text(row.get("options_verdict_v2")).upper()
    if v2 in {"CAUTION", "BEARISH_WARNING"}:
        add("high", f"Options flow caution: {v2}")
    elif text(row.get("options_verdict")).upper() == "CAUTION":
        add("medium", "Options flow caution signal")

    # Extension risk
    atr_pct = num(row.get("atrPct"))
    if atr_pct > 6.0:
        add("medium", f"High volatility — ATR {atr_pct:.1f}% of price")
    if not bool(row.get("above_20d_trend")):
        add("medium", "Price below 20-day trend")

    # Distance from 52w high
    dist_52w = number(row.get("pct_from_52wk_high") or row.get("distance_from_52w_high_pct"))
    if dist_52w is not None and dist_52w > -5:
        add("medium", f"Stock extended, only {abs(dist_52w):.1f}% from 52-week high")

    # VIX regime
    regime = text(row.get("market_regime") or row.get("regime")).upper()
    if regime == "CAUTION":
        add("low", "Market in CAUTION — trade at half size")
    elif regime == "RISK_OFF":
        add("high", "Market in RISK_OFF — consider sitting out")

    # Tape risk
    tape = text(row.get("tape_signal")).upper()
    if tape == "BEARISH":
        add("high", "Live tape showing seller aggression")

    # Seasonal headwind
    if text(row.get("seasonal_signal")).upper() == "HEADWIND":
        add("low", "Seasonal headwind for current month")

    # Earnings proximity
    days = number(row.get("earnings_in_days"))
    if days is not None and 0 < days <= 14:
        add("medium", f"Earnings in {int(days)} days — event risk")

    # Limit to 5 most severe
    severity_order = {"high": 0, "medium": 1, "low": 2}
    points.sort(key=lambda p: severity_order.get(p["severity"], 99))
    return points[:5]


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY QUALITY SCORE (computed at monitor time, not nightly)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_entry_quality_score(
    row: dict[str, Any],
    current_price: float | None = None,
    tape_signal: str | None = None,
    tape_available: bool = False,
) -> dict[str, Any]:
    """Compute entry quality score (0-100) based on real-time conditions."""
    zone_position = 0
    entry_low = num(
        (row.get("entryZone") or [None])[0]
        if isinstance(row.get("entryZone"), list)
        else row.get("entry")
    )
    entry_high = num(
        (row.get("entryZone") or [None, None])[1]
        if isinstance(row.get("entryZone"), list)
        else row.get("entry")
    )
    stop = num(row.get("stopLoss") or row.get("stop"))
    atr = num(row.get("atr14") or row.get("atr"))
    price = current_price if current_price is not None else num(row.get("price"))

    # Zone position score
    if price > 0 and entry_low > 0 and entry_high > 0:
        if entry_low <= price <= entry_high:
            zone_position = 40
        elif price < entry_low:
            dist_atr = (entry_low - price) / atr if atr > 0 else 999
            if dist_atr < 1.0:
                zone_position = 30
            elif dist_atr < 2.0:
                zone_position = 15
            else:
                zone_position = 5
        else:  # price > entry_high
            pct_above = (price - entry_high) / entry_high * 100
            if pct_above < 0.5:
                zone_position = 25
            elif pct_above < 2.0:
                zone_position = 10
            else:
                zone_position = 0
    else:
        zone_position = 20  # neutral assumption

    # Tape quality
    tape_score = 15  # neutral assumption
    if tape_available and tape_signal:
        ts = text(tape_signal).upper()
        if ts == "BULLISH":
            tape_score = 30
        elif ts == "NEUTRAL":
            tape_score = 15
        elif ts == "BEARISH":
            tape_score = 0

    # Staleness
    logged_at = text(row.get("logged_at") or row.get("date"))
    age_days = 0
    if logged_at:
        from datetime import datetime
        try:
            age_days = (datetime.now().date() - datetime.fromisoformat(logged_at).date()).days
        except Exception:
            age_days = 0

    if age_days <= 1:
        staleness = 20
    elif age_days <= 3:
        staleness = 12
    elif age_days <= 5:
        staleness = 5
    else:
        staleness = 0

    # Pre-market status
    pre_market = 5  # not checked yet
    if bool(row.get("pre_market_gap_favourable")):
        pre_market = 10
    elif bool(row.get("pre_market_gap_adverse")):
        pre_market = 0
    elif bool(row.get("pre_market_checked_at")):
        pre_market = 5  # checked but neutral

    total = zone_position + tape_score + staleness + pre_market
    total = min(100, total)

    label = "DO NOT ENTER"
    if total >= 70:
        label = "ENTER NOW"
    elif total >= 50:
        label = "WAIT"
    elif total >= 30:
        label = "WATCH ONLY"

    return {
        "entry_quality_score": total,
        "entry_quality_label": label,
        "entry_quality_breakdown": {
            "zone_position": zone_position,
            "tape_quality": tape_score,
            "staleness": staleness,
            "pre_market_status": pre_market,
        },
    }


def combined_display_label(trade_quality: int, entry_quality: int, age_days: int) -> str:
    """Generate combined display label for dashboard."""
    if age_days >= 6:
        return "⚫ STALE — REVIEW OR EXPIRE"

    tq_label = "WEAK"
    if trade_quality >= 75:
        tq_label = "HIGH"
    elif trade_quality >= 65:
        tq_label = "GOOD"
    elif trade_quality >= 55:
        tq_label = "MODERATE"

    eq_label = text(
        "ENTER NOW" if entry_quality >= 70
        else "WAIT" if entry_quality >= 50
        else "WATCH ONLY" if entry_quality >= 30
        else "DO NOT ENTER"
    )

    if tq_label == "HIGH" and eq_label == "ENTER NOW":
        return "🟢 READY"
    if tq_label == "HIGH" and eq_label == "WAIT":
        return "🟡 GREAT IDEA / WAIT FOR ENTRY"
    if tq_label == "GOOD" and eq_label == "ENTER NOW":
        return "🟢 GOOD SETUP / IN ZONE"
    if tq_label == "GOOD" and eq_label in {"WATCH ONLY", "DO NOT ENTER"}:
        return "🔵 WATCH ONLY"
    if tq_label == "MODERATE":
        return "🟠 HIGH RISK / ONLY WATCH"
    return "🔴 AVOID"
