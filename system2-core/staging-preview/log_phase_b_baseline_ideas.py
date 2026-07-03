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


def build_payload(setup: dict, run_date: str) -> dict:
    entry_zone = setup.get("entryZone") or []
    entry = setup.get("price")
    if isinstance(entry_zone, list) and entry_zone:
        entry = entry_zone[0]

    cluster = setup.get("cluster") or {}
    momentum = setup.get("momentum") or {}

    return {
        "date": run_date,
        "ticker": setup["symbol"],
        "mode": "SWING",
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
        "cluster_actual_risk": cluster.get("actualRiskDollars"),
        "cluster_shares": cluster.get("shares"),
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
        "council_verdict": setup.get("council_verdict"),
        "council_reasoning": setup.get("council_reasoning"),
        "council_upgrade": setup.get("council_upgrade") is True,
        "council_skip": setup.get("council_skip") is True,
        "models_used": setup.get("models_used") or [],
        "council_tier": setup.get("council_verdict"),
        "council_reasons": setup.get("council_reasoning"),
        "council_votes": setup.get("council_votes"),
        "council_conf": setup.get("council_conf"),
        "council_claude": setup.get("council_claude"),
        "council_gpt": setup.get("council_gpt"),
        "council_gemini": setup.get("council_gemini"),
        "council_red_flags": setup.get("council_red_flags") or [],
        "council_upgrade_sigs": setup.get("council_upgrade_sigs") or [],
        "council_force_skip": setup.get("council_force_skip") is True,
        "council_gates_trades": False,
        "chronos_dir": setup.get("chronos_dir"),
        "chronos_conf": setup.get("chronos_conf") if setup.get("chronos_conf") is not None else setup.get("forecastConviction"),
        "chronos_band_pct": setup.get("chronos_band_pct"),
        # v2 3-category scoring
        "core_setup_score": setup.get("core_setup_score"),
        "core_setup_breakdown": setup.get("core_setup_breakdown"),
        "confirmation_score": setup.get("confirmation_score"),
        "confirmation_breakdown": setup.get("confirmation_breakdown"),
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
    }


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

    # Upgrade 3 — No-Trade-Today Threshold
    quality_ideas = [s for s in finalists if (s.get("trade_quality_score") or 0) >= 65]
    watchlist_ideas = [s for s in finalists if 55 <= (s.get("trade_quality_score") or 0) < 65]
    no_trade_day = len(quality_ideas) == 0
    low_idea_day = 1 <= len(quality_ideas) <= 3
    regime = os.environ.get("SYSTEM2_REGIME", "")
    vix = os.environ.get("SYSTEM2_VIX_CURRENT", "")

    telegram_results: list[dict] = []

    if no_trade_day and not args.dry_run:
        msg = (
            f"⚫ NO TRADE TODAY\n"
            f"System found 0 high-quality setups.\n"
            f"Conditions: VIX {vix} | Regime {regime}\n"
            f"SPY: {os.environ.get('SYSTEM2_SPY_1D_PCT', '-')}%")
        telegram_results.append(send_telegram_alert(msg))
        # Still post watchlist ideas to dashboard but mark them
        for s in watchlist_ideas:
            s["_watchlist_only"] = True
    elif low_idea_day and not args.dry_run:
        msg = (
            f"⚠️ LOW CONVICTION DAY — only {len(quality_ideas)} quality ideas.\n"
            f"Trade with caution. Half size recommended.")
        if regime == "CAUTION" and len(quality_ideas) < 5:
            msg += "\nMarket in CAUTION + limited ideas. Consider sitting out today."
        telegram_results.append(send_telegram_alert(msg))

    results = []
    for setup in finalists:
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


if __name__ == "__main__":
    main()
