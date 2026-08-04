#!/usr/bin/env python3
"""
Log Phase B bare-core finalists to the scoring loop.

This posts each Stage 7 clustered finalist to:
  http://72.62.134.167:3210/api/idea

It logs paper-mode ideas only. Scanner-discovered ideas remain
source="scanner"; top-of-funnel catalyst candidates that survive the same
funnel are logged source="catalyst" with sub_type/catalyst fields. No broker
calls and no live trading. Chronos/options ride along for attribution only;
AI council fields stay off.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from idea_lifecycle import record_stage
except Exception:
    def record_stage(*args, **kwargs):  # type: ignore
        return None


def send_telegram_alert(text: str) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return {"sent": False, "reason": "missing telegram credentials"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"sent": True, "status": resp.status}
    except Exception as e:
        return {"sent": False, "error": str(e)}


ROOT = Path(__file__).resolve().parent
STAGE5_INPUT = ROOT / "stage5_news_safe_finalists.json"
STAGE7_INPUT = ROOT / "stage7_clustered_survivors.json"
DEFAULT_INPUT = STAGE5_INPUT if STAGE5_INPUT.exists() else STAGE7_INPUT
DEFAULT_OUTPUT = ROOT / "phase_b_baseline_idea_log_results.json"
DEFAULT_URL = "http://72.62.134.167:3210/api/idea"

# Confluence overlay — family_scores etc. are computed in confluence_scoring.py
# but B4 reads council data first, so stage7 may be missing them.
CONFLUENCE_PATH = ROOT / "stage2_confluence_ranked_top40.json"

def load_confluence_overlay() -> dict[str, dict]:
    if not CONFLUENCE_PATH.exists():
        return {}
    try:
        data = json.loads(CONFLUENCE_PATH.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else (data.get("ideas") or data.get("candidates") or [])
        return {
            str(row.get("symbol") or row.get("ticker") or "").upper(): row
            for row in rows
        }
    except Exception:
        return {}


SOCIAL_SENTIMENT_PATH = ROOT / "data" / "social_sentiment.json"

def load_social_sentiment_overlay() -> dict[str, dict]:
    if not SOCIAL_SENTIMENT_PATH.exists():
        return {}
    try:
        data = json.loads(SOCIAL_SENTIMENT_PATH.read_text(encoding="utf-8"))
        today = datetime.now(timezone.utc).date().isoformat()
        if data.get("date") and data.get("date") != today:
            return {}
        tickers = data.get("tickers", {}) if isinstance(data, dict) else {}
        return {str(k).upper(): v for k, v in tickers.items() if isinstance(v, dict)}
    except Exception:
        return {}


def council_vote_count(setup: dict) -> int | None:
    verdicts = [
        setup.get("claude_verdict"),
        setup.get("gemini_verdict"),
        setup.get("gpt4o_verdict"),
    ]
    usable = [str(v or "").upper() for v in verdicts if v not in (None, "")]
    if not usable:
        return None
    supportive = {"UPGRADE", "TIER1", "TIER2", "TIER3"}
    return sum(1 for v in usable if v in supportive)


def council_confidence_score(setup: dict) -> int | None:
    values = []
    weights = {"HIGH": 90, "MEDIUM": 65, "LOW": 35}
    for key in ("claude_confidence", "gemini_confidence", "gpt4o_confidence"):
        value = str(setup.get(key) or "").upper()
        if value in weights:
            values.append(weights[value])
    if not values:
        return None
    return round(sum(values) / len(values))


DANELFIN_PATH = ROOT / "data" / "danelfin_scores.json"

def load_danelfin_lookup() -> dict[str, dict]:
    if not DANELFIN_PATH.exists():
        return {}
    try:
        data = json.loads(DANELFIN_PATH.read_text(encoding="utf-8"))
        scores = data.get("scores", data)
        return {str(k).upper(): v for k, v in (scores or {}).items() if isinstance(v, dict)}
    except Exception:
        return {}


def post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "system2-phase-b-baseline-logger/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {"status": resp.status}


def compute_basic_family_scores(setup: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Fallback family scoring for ideas not in confluence top-40."""
    # Family B — positioning/flow
    options = str(setup.get("options_verdict", ""))
    options_score = 20 if "STRONG_CONFIRM" in options else 12 if "CONFIRM" in options else 0
    dp = str(setup.get("dark_pool_signal", ""))
    dp_score = 15 if "STRONG" in dp else 8 if "MODERATE" in dp else 0
    family_b = min(35, options_score + dp_score)

    # Family C — catalyst
    danelfin = setup.get("danelfin_ai_score") or 0
    d_score = 8 if danelfin >= 9 else 5 if danelfin >= 7 else 0
    squeeze = setup.get("short_squeeze_score", 0) or 0
    sq_score = 10 if squeeze >= 70 else 6 if squeeze >= 50 else 0
    family_c = min(20, d_score + sq_score)

    # Family D — structural
    gex = str(setup.get("gex_regime", ""))
    gex_score = 15 if "BULLISH" in gex else 8 if "POSITIVE" in gex else 0
    family_d = min(20, gex_score)

    # Family A — momentum
    family_a = min(25, setup.get("x_signal_score", 0) or 0)

    total = family_a + family_b + family_c + family_d
    families_firing = sum([
        1 if family_a >= 5 else 0,
        1 if family_b >= 5 else 0,
        1 if family_c >= 5 else 0,
        1 if family_d >= 5 else 0,
    ])

    return {
        "momentum": family_a,
        "positioning": family_b,
        "catalyst": family_c,
        "structural": family_d,
        "families_firing": families_firing,
        "source": "fallback_compute",
    }, total


def build_payload(setup: dict, run_date: str) -> dict:
    entry_zone = setup.get("entryZone") or []
    entry = setup.get("price")
    if isinstance(entry_zone, list) and entry_zone:
        entry = entry_zone[0]

    cluster = setup.get("cluster") or {}
    momentum = setup.get("momentum") or {}

    payload = {
        "date": run_date,
        "ticker": setup["symbol"],
        "mode": "SWING",
        "idea_stream": "swing_momentum",
        "hold_period": "3-10 day",
        "paper": True,
        "source": setup.get("source") or "scanner",
        "regime": os.environ.get("SYSTEM2_REGIME") or setup.get("regime"),
        "market_regime": os.environ.get("SYSTEM2_REGIME") or setup.get("market_regime"),
        "regime_reason": os.environ.get("SYSTEM2_REGIME_REASON") or setup.get("regime_reason"),
        "spy_1d_pct": _num_env("SYSTEM2_SPY_1D_PCT", setup.get("spy_1d_pct")),
        "qqq_1d_pct": _num_env("SYSTEM2_QQQ_1D_PCT", setup.get("qqq_1d_pct")),
        "vix_current": _num_env("SYSTEM2_VIX_CURRENT", setup.get("vix_current")),
        "vix_1d_chg": _num_env("SYSTEM2_VIX_1D_CHG", setup.get("vix_1d_chg")),
        "sub_type": setup.get("sub_type"),
        "sub_types": setup.get("sub_types"),
        "catalyst_summary": setup.get("catalyst_summary"),
        "catalyst_date": setup.get("catalyst_date"),
        "catalyst_datetime": setup.get("catalyst_datetime"),
        "catalyst_score": setup.get("catalyst_score"),
        "catalyst_sources": setup.get("catalyst_sources"),
        "entry": entry,
        "stop": setup.get("stopLoss"),
        "target": setup.get("tp1"),
        "sector": setup.get("sector"),
        "setup": setup.get("setup") or setup.get("setupType"),
        "grade": setup.get("grade"),
        "price": setup.get("price"),
        "vwap": setup.get("vwap"),
        "entryZone": setup.get("entryZone"),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
        "volumeRatio": setup.get("volumeRatio"),
        "rsVsSpy": setup.get("rsVsSpy"),
        "convictionScore": setup.get("convictionScore") or setup.get("setupQualityScore"),
        "setup_score": setup.get("setup_score") if setup.get("setup_score") is not None else setup.get("setupQualityScore"),
        "confluence_score": setup.get("confluence_score"),
        "confluence_bonuses": setup.get("confluence_bonuses") or [],
        "action": setup.get("action") or "WATCH",
        "atr14": setup.get("atr14"),
        "atrPct": setup.get("atrPct"),
        "distanceFromVWAP": setup.get("distanceFromVWAP"),
        "sectorAlpha": setup.get("sectorAlpha"),
        "sectorGateOpen": setup.get("sectorGateOpen"),
        "scoreReasons": setup.get("scoreReasons") or [],
        "options_verdict": setup.get("options_verdict"),
        "options_signals_count": setup.get("options_signals_count"),
        "iv_rank": setup.get("iv_rank_proxy") if setup.get("iv_rank_proxy") is not None else setup.get("iv_rank"),
        "vol_oi_ratio": setup.get("call_vol_oi_ratio") if setup.get("call_vol_oi_ratio") is not None else setup.get("vol_oi_ratio"),
        "put_call_vol_ratio": setup.get("put_call_vol_ratio"),
        "call_oi_skew": setup.get("call_oi_skew"),
        "options_notes": setup.get("options_notes"),
        # Options structure
        "max_pain_weekly": setup.get("max_pain_weekly"),
        "max_pain_monthly": setup.get("max_pain_monthly"),
        "max_pain_expiry_weekly": setup.get("max_pain_expiry_weekly"),
        "max_pain_expiry_monthly": setup.get("max_pain_expiry_monthly"),
        "days_to_weekly_expiry": setup.get("days_to_weekly_expiry"),
        "price_vs_max_pain_pct": setup.get("price_vs_max_pain_pct"),
        "max_pain_signal": setup.get("max_pain_signal"),
        "call_wall_1": setup.get("call_wall_1"),
        "call_wall_1_oi": setup.get("call_wall_1_oi"),
        "call_wall_2": setup.get("call_wall_2"),
        "call_wall_2_oi": setup.get("call_wall_2_oi"),
        "put_wall_1": setup.get("put_wall_1"),
        "put_wall_1_oi": setup.get("put_wall_1_oi"),
        "put_wall_2": setup.get("put_wall_2"),
        "put_wall_2_oi": setup.get("put_wall_2_oi"),
        "tp1_wall_conflict": setup.get("tp1_wall_conflict"),
        "tp1_wall_note": setup.get("tp1_wall_note"),
        "tp1_adjusted": setup.get("tp1_adjusted"),
        "stop_wall_support": setup.get("stop_wall_support"),
        "stop_wall_note": setup.get("stop_wall_note"),
        "gamma_flip_proxy": setup.get("gamma_flip_proxy"),
        "price_vs_gamma_flip": setup.get("price_vs_gamma_flip"),
        "news_safety_status": setup.get("news_safety_status"),
        "news_recent_items_checked": setup.get("news_recent_items_checked"),
        "hard_landmine": setup.get("hard_landmine"),
        "analyst_change": setup.get("analyst_change"),
        "cluster_sector": cluster.get("sector"),
        "cluster_etf": cluster.get("etf"),
        "cluster_size_before": cluster.get("clusterSizeBefore"),
        "cluster_rank": cluster.get("clusterRank"),
        "cluster_risk_budget": cluster.get("clusterRiskBudget"),
        "cluster_allocated_risk": cluster.get("allocatedRiskDollars"),
        "cluster_base_allocated_risk": cluster.get("baseAllocatedRiskDollars"),
        "cluster_actual_risk": cluster.get("actualRiskDollars"),
        "cluster_shares": cluster.get("shares"),
        "size_rule": setup.get("size_rule"),
        "size_rule_multiplier": setup.get("size_rule_multiplier"),
        "size_rule_reason": setup.get("size_rule_reason"),
        "intake_source_layer": setup.get("intake_source_layer") or setup.get("source_layer") or setup.get("universe_source"),
        "source_layer": setup.get("source_layer") or setup.get("intake_source_layer") or setup.get("universe_source"),
        "normal_nonpmf_halfsize_enabled": setup.get("normal_nonpmf_halfsize_enabled"),
        "normal_nonpmf_halfsize_deployed_at": setup.get("normal_nonpmf_halfsize_deployed_at"),
        "normal_nonpmf_halfsize_reeval_threshold": setup.get("normal_nonpmf_halfsize_reeval_threshold"),
        "momentum_bull_stack": momentum.get("bullStack"),
        "momentum_rsi14": momentum.get("rsi14"),
        "momentum_adx14": momentum.get("adx14"),
        "momentum_close_position": momentum.get("closePosition"),
        "momentum_near_20bar_high": momentum.get("near20BarHigh"),
        "chronos_status": setup.get("chronos_status"),
        "chronos2_1d": setup.get("chronos2_1d"),
        "chronos2_3d": setup.get("chronos2_3d"),
        "chronos2_5d": setup.get("chronos2_5d"),
        "forecastConviction": setup.get("forecastConviction"),
        "forecastDecision": setup.get("forecastDecision"),
        "forecastTier": setup.get("forecastTier"),
        "forecastReasons": setup.get("forecastReasons"),
        "council_verdict": setup.get("council_verdict") or setup.get("council_final_verdict"),
        "council_reasoning": setup.get("council_reasoning"),
        "council_upgrade": setup.get("council_upgrade") is True,
        "council_skip": setup.get("council_skip") is True,
        "models_used": setup.get("models_used") or [],
        "council_tier": setup.get("council_verdict") or setup.get("council_final_verdict"),
        "council_reasons": setup.get("council_reasoning"),
        "council_votes": setup.get("council_votes") if setup.get("council_votes") is not None else council_vote_count(setup),
        "council_conf": setup.get("council_conf") if setup.get("council_conf") is not None else council_confidence_score(setup),
        "council_claude": setup.get("council_claude") or setup.get("claude_verdict"),
        "council_gpt": setup.get("council_gpt") or setup.get("gpt4o_verdict"),
        "council_gemini": setup.get("council_gemini") or setup.get("gemini_verdict"),
        "council_red_flags": setup.get("council_red_flags") or [],
        "council_upgrade_sigs": setup.get("council_upgrade_sigs") or [],
        "council_force_skip": setup.get("council_force_skip") is True,
        "council_final_verdict": setup.get("council_final_verdict"),
        "council_gates_trades": False,
        "chronos_dir": setup.get("chronos_dir"),
        "chronos_conf": setup.get("chronos_conf") if setup.get("chronos_conf") is not None else setup.get("forecastConviction"),
        "chronos_band_pct": setup.get("chronos_band_pct"),
        # v2 3-category scoring
        "core_setup_score": setup.get("core_setup_score"),
        "core_setup_breakdown": setup.get("core_setup_breakdown"),
        "confirmation_score": setup.get("confirmation_score"),
        "confirmation_breakdown": setup.get("confirmation_breakdown"),
        "family_scores": setup.get("family_scores"),
        "families_firing": setup.get("families_firing"),
        "risk_score": setup.get("risk_score"),
        "risk_breakdown": setup.get("risk_breakdown"),
        "trade_quality_score": setup.get("trade_quality_score"),
        "trade_quality_label": setup.get("trade_quality_label"),
        "trade_quality_finalist": setup.get("trade_quality_finalist"),
        # Data quality
        "data_quality_score": setup.get("data_quality_score"),
        "data_quality_label": setup.get("data_quality_label"),
        # Bear case
        "bear_case_points": setup.get("bear_case_points") or [],
        # Council v2 per-model verdicts
        "claude_verdict": setup.get("claude_verdict"),
        "kimi_verdict": setup.get("kimi_verdict"),
        "gemini_verdict": setup.get("gemini_verdict"),
        "gpt4o_verdict": setup.get("gpt4o_verdict"),
        "kimi_r2_verdict": setup.get("kimi_r2_verdict"),
        "gemini_r2_verdict": setup.get("gemini_r2_verdict"),
        "gpt4o_r2_verdict": setup.get("gpt4o_r2_verdict"),
        # News / analyst (Upgrade 7)
        "news_risk": setup.get("news_risk"),
        "news_sentiment": setup.get("news_sentiment"),
        "news_flag_count": setup.get("news_flag_count"),
        "analyst_signal": setup.get("analyst_signal"),
        "analyst_upgrades_7d": setup.get("analyst_upgrades_7d"),
        "analyst_downgrades_7d": setup.get("analyst_downgrades_7d"),
        "consistent_beater": setup.get("consistent_beater"),
        # Seasonality + dark pool
        "seasonal_signal": setup.get("seasonal_signal"),
        "dark_pool_signal": setup.get("dark_pool_signal"),
        "dark_pool_pct": setup.get("dark_pool_pct"),
        "dark_pool_source": setup.get("dark_pool_source", "proxy"),
        # Unified social sentiment
        "social_sentiment": setup.get("social_sentiment", "NO_DATA"),
        "social_score": setup.get("social_score", 0),
        "social_sources": setup.get("social_sources", []),
        "social_bull_pct": setup.get("social_bull_pct", 0),
        "getxapi_active": setup.get("getxapi_active", False),
        "stocktwits_bull_pct": setup.get("stocktwits_bull_pct"),
        "stocktwits_message_count": setup.get("stocktwits_message_count"),
        "stocktwits_messages": setup.get("stocktwits_messages"),
        "stocktwits_sentiment": setup.get("stocktwits_sentiment", "NO_DATA"),
        "reddit_bull_pct": setup.get("reddit_bull_pct"),
        "reddit_high_quality_posts": setup.get("reddit_high_quality_posts"),
        "reddit_post_count": setup.get("reddit_post_count"),
        "getxapi_bull_pct": setup.get("getxapi_bull_pct"),
        "getxapi_tweet_count": setup.get("getxapi_tweet_count"),
        # Set 2 origin tracking
        "set": setup.get("set", 1),
        "set_source": setup.get("set_source", "technical_momentum"),
        "multi_set_idea": setup.get("multi_set_idea", False),
        "multi_set_sets": setup.get("multi_set_sets") or [1],
        "multi_set_bonus": setup.get("multi_set_bonus", 0),
        "set2_options_score": setup.get("set2_options_score"),
        "set2_trade_quality_score": setup.get("set2_trade_quality_score"),
        "set2_entry_rules": setup.get("set2_entry_rules"),
        "ask_side_sweep_count": setup.get("ask_side_sweep_count"),
        "bullish_premium_total": setup.get("bullish_premium_total"),
        "repeat_flow_count": setup.get("repeat_flow_count"),
        # Prompt 2 — performance tracking fields
        "era": "system2_v2",
        "idea_entry_price": setup.get("pre_market_price") if setup.get("pre_market_price") else (round((setup.get("entryZone",[setup.get("price")])[0] + setup.get("entryZone",[setup.get("price")])[-1])/2, 4) if isinstance(setup.get("entryZone"), list) and len(setup.get("entryZone"))>=2 else setup.get("price")),
        "idea_tracking_source": "pre_market" if setup.get("pre_market_price") else ("zone_midpoint" if isinstance(setup.get("entryZone"), list) and len(setup.get("entryZone"))>=2 else "fmp_scan"),
        "trade_entered": False,
        "slippage_pct": 0.15,
        "slippage_r": None,
        "trade_r_gross": None,
        "trade_r_net": None,
        "idea_r": None,
        "idea_outcome": None,
        "mfe_r": None,
        "mae_r": None,
        "capture_rate": None,
        # Signal fields
        "rvol_tier": setup.get("rvol_tier"),
        "dark_pool_signal": setup.get("dark_pool_signal") or setup.get("darkPoolSignal"),
        "dark_pool_pct": setup.get("dark_pool_pct"),
        "dark_pool_source": setup.get("dark_pool_source", "proxy"),
        "insider_buy_signal": setup.get("insider_buy_signal"),
        "insider_buy_value": setup.get("insider_buy_value"),
        "insider_buy_count": setup.get("insider_buy_count"),
        "is_cluster_buy": setup.get("is_cluster_buy", False),
        "insider_data_source": setup.get("insider_data_source", "none"),
        "congress_signal": setup.get("congress_signal", "NONE"),
        "congress_buy_count": setup.get("congress_buy_count", 0),
        "congress_politicians": setup.get("congress_politicians", []),
        "short_squeeze_score": setup.get("short_squeeze_score"),
        "short_percent_float": setup.get("short_percent_float") or setup.get("shortPercentFloat"),
        "pead_score": setup.get("pead_score", 0),
        "pead_signals": setup.get("pead_signals", []),
        "earnings_surprise_pct": setup.get("earnings_surprise_pct"),
        "days_since_earnings": setup.get("days_since_earnings"),
        "earnings_surprise_is_beat": setup.get("earnings_surprise_is_beat"),
        "earnings_surprise_source": setup.get("earnings_surprise_source"),
        "estimate_revision": setup.get("estimate_revision"),
        "analyst_rec_direction": setup.get("analyst_rec_direction"),
        "analyst_bull_pct": setup.get("analyst_bull_pct"),
        "fda_event_flag": setup.get("fda_event_flag", False),
        "tape_signal": setup.get("tape_signal"),
        "gex_regime": setup.get("gex_regime"),
        # Prompt 3 — portfolio exposure fields
        "position_r": setup.get("position_r") if setup.get("position_r") is not None else 1.0,
        "entered_at": None,
    }

    # Danelfin enrichment
    ticker = setup.get("symbol", "")
    danelfin_lookup = load_danelfin_lookup()
    d_data = danelfin_lookup.get(str(ticker).upper(), {})
    if d_data:
        payload.setdefault("danelfin_ai_score", d_data.get("ai_score"))
        payload.setdefault("danelfin_data_available", True)
        payload.setdefault("danelfin_technical", d_data.get("technical"))
        payload.setdefault("danelfin_fundamental", d_data.get("fundamental"))
        payload.setdefault("danelfin_sentiment", d_data.get("sentiment"))

    return payload


def _num_env(key: str, fallback=None):
    raw = os.environ.get(key)
    if raw in (None, ""):
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    finalists = json.loads(Path(args.input).read_text(encoding="utf-8"))

    # Overlay confluence family_scores / confirmation_score / trade_quality_score
    # when B4 reads council data that missed the confluence scoring step.
    confluence_overlay = load_confluence_overlay()
    for setup in finalists:
        sym = str(setup.get("symbol") or setup.get("ticker") or "").upper()
        if sym in confluence_overlay:
            overlay = confluence_overlay[sym]
            for key in ("family_scores", "confirmation_score", "confirmation_breakdown",
                        "trade_quality_score", "trade_quality_label", "trade_quality_finalist",
                        "core_setup_score", "core_setup_breakdown",
                        "risk_score", "risk_breakdown",
                        "data_quality_score", "data_quality_label", "data_quality_checks",
                        "market_regime", "market_regime_detected", "regime_caps_applied", "regime_weights_applied",
                        "bear_case_points",
                        "analyst_rec_direction", "analyst_bull_pct", "fda_event_flag",
                        "earnings_surprise_source"):
                if key in overlay and setup.get(key) is None:
                    setup[key] = overlay[key]

    # Overlay unified social sentiment fields from social_scraper.py --enrich
    social_overlay = load_social_sentiment_overlay()
    for setup in finalists:
        sym = str(setup.get("symbol") or setup.get("ticker") or "").upper()
        if sym in social_overlay:
            overlay = social_overlay[sym]
            setup["social_sentiment"] = overlay.get("social_sentiment", "NO_DATA")
            setup["social_score"] = overlay.get("score", overlay.get("social_score", 0))
            setup["social_sources"] = overlay.get("sources_used", overlay.get("social_sources", []))
            setup["social_bull_pct"] = overlay.get("social_combined_pct", overlay.get("social_bull_pct", 0))
            setup["getxapi_active"] = overlay.get("getxapi_active", False)

            st = overlay.get("stocktwits") or {}
            setup["stocktwits_bull_pct"] = st.get("bull_pct", overlay.get("stocktwits_bull_pct", 0))
            setup["stocktwits_message_count"] = st.get("message_count", overlay.get("stocktwits_message_count", 0))
            setup["stocktwits_messages"] = st.get("message_count", overlay.get("stocktwits_messages", 0))
            setup["stocktwits_sentiment"] = st.get("sentiment", overlay.get("stocktwits_sentiment", "NO_DATA"))

            rd = overlay.get("reddit") or {}
            setup["reddit_bull_pct"] = rd.get("bull_pct", overlay.get("reddit_bull_pct", 0))
            setup["reddit_high_quality_posts"] = rd.get("high_quality_posts", overlay.get("reddit_high_quality_posts", 0))
            setup["reddit_post_count"] = rd.get("post_count", overlay.get("reddit_post_count", 0))

            gx = overlay.get("getxapi") or {}
            setup["getxapi_bull_pct"] = gx.get("bull_pct", overlay.get("getxapi_bull_pct", 0))
            setup["getxapi_tweet_count"] = gx.get("tweet_count", overlay.get("getxapi_tweet_count", 0))

    # Fallback family_scores for finalists not in confluence top-40 (e.g., Set 2)
    for setup in finalists:
        if not setup.get("family_scores") or (
            not setup["family_scores"].get("momentum") and
            not setup["family_scores"].get("positioning")
        ):
            family_scores, conf_score = compute_basic_family_scores(setup)
            setup["family_scores"] = family_scores
            if setup.get("confirmation_score") is None:
                setup["confirmation_score"] = conf_score

    # Lifecycle: every finalist has been SCORED by this point.
    for setup in finalists:
        try:
            record_stage(
                ticker=setup.get("symbol") or setup.get("ticker"),
                date=args.date,
                stage="SCORED",
                detail={
                    "source": setup.get("source", "scanner"),
                    "setup_score": setup.get("setup_score"),
                    "confluence_score": setup.get("confluence_score"),
                    "trade_quality_score": setup.get("trade_quality_score"),
                    "core_setup_score": setup.get("core_setup_score"),
                    "confirmation_score": setup.get("confirmation_score"),
                    "risk_score": setup.get("risk_score"),
                    "data_quality_score": setup.get("data_quality_score"),
                    "family_scores": setup.get("family_scores"),
                    "set": setup.get("set", 1),
                    "multi_set_idea": setup.get("multi_set_idea", False),
                },
            )
        except Exception:
            pass

    # Upgrade 3 — No-Trade-Today Threshold
    quality_ideas = [s for s in finalists if (s.get("trade_quality_score") or 0) >= 65]
    watchlist_ideas = [s for s in finalists if 55 <= (s.get("trade_quality_score") or 0) < 65]
    no_trade_day = len(quality_ideas) == 0
    low_idea_day = 1 <= len(quality_ideas) <= 3
    regime = os.environ.get("SYSTEM2_REGIME", "")
    vix = os.environ.get("SYSTEM2_VIX_CURRENT", "")

    telegram_results: list[dict] = []

    danelfin_f, total_f = _load_danelfin_finalist_count(finalists)
    danelfin_line = f"\n📊 Danelfin: {danelfin_f}/{total_f} finalists scored"

    set2_line = _set2_line(finalists)

    if no_trade_day and not args.dry_run:
        msg = (
            f"⚫ NO TRADE TODAY\n"
            f"System found 0 high-quality setups.\n"
            f"Conditions: VIX {vix} | Regime {regime}\n"
            f"SPY: {os.environ.get('SYSTEM2_SPY_1D_PCT', '-')}%"
            + danelfin_line + set2_line)
        telegram_results.append(send_telegram_alert(msg))
        # Still post watchlist ideas to dashboard but mark them
        for s in watchlist_ideas:
            s["_watchlist_only"] = True
    elif low_idea_day and not args.dry_run:
        msg = (
            f"⚠️ LOW CONVICTION DAY — only {len(quality_ideas)} quality ideas.\n"
            f"Trade with caution. Half size recommended."
            + danelfin_line + set2_line)
        if regime == "CAUTION" and len(quality_ideas) < 5:
            msg += "\nMarket in CAUTION + limited ideas. Consider sitting out today."
        telegram_results.append(send_telegram_alert(msg))

    results = []
    for setup in finalists:
        # Lifecycle: this idea is a FINALIST being posted to the dashboard.
        try:
            record_stage(
                ticker=setup.get("symbol") or setup.get("ticker"),
                date=args.date,
                stage="FINALIST",
                detail={
                    "source": setup.get("source", "scanner"),
                    "tier": setup.get("trade_readiness_tier") or setup.get("grade"),
                    "trade_quality_score": setup.get("trade_quality_score"),
                    "trade_quality_label": setup.get("trade_quality_label"),
                    "passed_gates": True,
                    "set": setup.get("set", 1),
                    "multi_set_idea": setup.get("multi_set_idea", False),
                },
            )
        except Exception:
            pass

        payload = build_payload(setup, args.date)
        record = {
            "ticker": payload["ticker"],
            "payload": payload,
            "posted": False,
            "response": None,
            "error": None,
        }
        if args.dry_run:
            record["response"] = {"dryRun": True}
        else:
            try:
                record["response"] = post_json(args.url, payload)
                record["posted"] = bool(record["response"].get("ok"))
            except urllib.error.HTTPError as exc:
                record["error"] = f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:500]}"
            except Exception as exc:
                record["error"] = str(exc)
        results.append(record)

    summary = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "date": args.date,
        "dryRun": args.dry_run,
        "inputCount": len(finalists),
        "postedCount": sum(1 for r in results if r["posted"]),
        "errorCount": sum(1 for r in results if r["error"]),
        "no_trade_day": no_trade_day,
        "low_idea_day": low_idea_day,
        "quality_idea_count": len(quality_ideas),
        "watchlist_count": len(watchlist_ideas),
        "telegram_results": telegram_results,
        "results": results,
    }
    Path(args.output).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({
        "inputCount": summary["inputCount"],
        "postedCount": summary["postedCount"],
        "errorCount": summary["errorCount"],
        "no_trade_day": no_trade_day,
        "low_idea_day": low_idea_day,
        "quality_idea_count": len(quality_ideas),
        "tickers": [r["ticker"] for r in results],
    }, indent=2))




def _load_danelfin_finalist_count(finalists: list[dict]) -> tuple[int, int]:
    """Return (danelfin_finalists, total_finalists) from danelfin_scores.json."""
    try:
        data = json.loads(Path("/root/system2-core/data/danelfin_scores.json").read_text(encoding="utf-8"))
        scores = data.get("scores", {})
        danelfin_count = sum(1 for f in finalists if f.get("symbol") in scores)
        return danelfin_count, len(finalists)
    except Exception:
        return 0, len(finalists)


def _set2_breakdown(finalists: list[dict]) -> dict[str, int]:
    """Return counts by set origin."""
    set1 = sum(1 for f in finalists if (f.get("set") == 1 and not f.get("multi_set_idea")))
    set2 = sum(1 for f in finalists if (f.get("set") == 2 and not f.get("multi_set_idea")))
    multi = sum(1 for f in finalists if f.get("multi_set_idea"))
    return {"set1": set1, "set2": set2, "multi": multi}


def _set2_line(finalists: list[dict]) -> str:
    b = _set2_breakdown(finalists)
    lines = [f"S1 Technical: {b['set1']} | S2 Options Flow: {b['set2']}"]
    if b["multi"]:
        lines.append(f"Multi-Set: {b['multi']}")
    if b["set2"] == 0:
        lines.append("Set 2: quiet options tape")
    return "\n" + " | ".join(lines)

if __name__ == "__main__":
    main()
