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
from pathlib import Path
from typing import Any


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
        return 0
    return 5


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
    """0-15 based on dark pool proxy signal."""
    sig = text(row.get("dark_pool_signal")).upper()
    if sig == "STRONG":
        return 15
    if sig == "MODERATE":
        return 10
    if sig == "WEAK":
        return 5
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


def compute_confirmation_score(row: dict[str, Any]) -> dict[str, Any]:
    """Return confirmation_score (0-100) plus component breakdown."""
    options = compute_options_conf(row)
    chronos = compute_chronos_conf(row)
    seasonal = compute_seasonal_conf(row)
    dark_pool = compute_dark_pool_conf(row)
    x_signal = compute_x_signal_conf(row)
    analyst = compute_analyst_conf(row)

    # Cap each category
    options = min(options, 25)
    chronos = min(chronos, 20)
    seasonal = min(seasonal, 15)
    dark_pool = min(dark_pool, 15)
    x_signal = min(x_signal, 15)
    analyst = min(analyst, 10)

    # Options structure bonus/penalty (capped +5 / -4)
    options_structure = compute_options_structure_conf(row)

    total = options + chronos + seasonal + dark_pool + x_signal + analyst + options_structure
    # Cap total confirmation at 100 (theoretical max 105 with structure bonus)
    total = min(100, total)
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
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RISK SCORE (0-100, higher = more risk)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_earnings_risk(row: dict[str, Any]) -> int:
    """0-30 based on proximity to earnings."""
    days = number(row.get("earnings_in_days") or row.get("days_to_earnings"))
    if days is None:
        return 0
    if days < 8:
        return 30
    if days <= 14:
        return 15
    return 0


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

    # Distance from 52-week high if available
    dist_52w = number(row.get("distance_from_52w_high_pct"))
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

def compute_trade_quality(row: dict[str, Any], market_regime: str | None = None) -> dict[str, Any]:
    """Compute full 3-category scoring for a single idea row."""
    core = compute_core_setup_score(row)
    confirmation = compute_confirmation_score(row)
    risk = compute_risk_score(row, market_regime)

    trade_quality = round(
        core["core_setup_score"] * 0.50
        + confirmation["confirmation_score"] * 0.30
        - risk["risk_score"] * 0.20
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

    result = {
        **core,
        **confirmation,
        **risk,
        "trade_quality_score": trade_quality,
        "trade_quality_label": label,
        "trade_quality_finalist": trade_quality >= 55,  # advisory only; actual cutoff in confluence_scoring.py
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
    if provider == "impliedoptions_auth":
        score += 15
        checks["options_source"] = "primary"
    elif provider in {"yahoo", "alpha_vantage"}:
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
    has_dp = text(row.get("dark_pool_signal")).upper() not in {"", "INSUFFICIENT_DATA"}
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
    dist_52w = num(row.get("distance_from_52w_high_pct"))
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
