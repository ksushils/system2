#!/usr/bin/env python3
"""Persist the full scored pool for Signal Lab queries.

Reads the Stage 2 scored universe, runs the full 3-category trade-quality
scoring engine over every ticker, computes mandatory safety flags, marks
finalists, and writes data/scored_pool.json.

This step is intentionally placed after the pipeline has produced finalists
so that `is_finalist` can be derived from stage7_clustered_survivors.json,
but it scores the entire B2 survivor pool so users can query beyond the
logged finalists.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scoring_engine import (
    compute_trade_quality,
    get_danelfin_score,
    has_pending_fda_event,
)


ROOT = Path(__file__).resolve().parent
SCORED_PATH = ROOT / "stage2_surgical_strike_scored.json"
STAGE7_PATH = ROOT / "stage7_clustered_survivors.json"
SOCIAL_PATH = ROOT / "data" / "social_sentiment.json"
OUTPUT_PATH = ROOT / "data" / "scored_pool.json"
METADATA_PATH = ROOT / "data" / "scored_pool.metadata.json"


def text(value: Any) -> str:
    return str(value or "").strip()


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        v = float(value)
        return v if __import__("math").isfinite(v) else default
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_social_overlay() -> dict[str, dict]:
    data = load_json(SOCIAL_PATH)
    if not isinstance(data, dict):
        return {}
    today = datetime.now(timezone.utc).date().isoformat()
    if data.get("date") and data.get("date") != today:
        return {}
    tickers = data.get("tickers", {})
    return {str(k).upper(): v for k, v in tickers.items() if isinstance(v, dict)}


def compute_safety_flags(row: dict[str, Any]) -> dict[str, Any]:
    ticker = text(row.get("symbol") or row.get("ticker")).upper()

    earnings_days = row.get("earnings_in_days")
    try:
        earnings_days_int = int(earnings_days) if earnings_days is not None else None
    except Exception:
        earnings_days_int = None
    earnings_within_7d = bool(earnings_days_int is not None and 0 <= earnings_days_int <= 7)

    fda_event_pending = bool(ticker and has_pending_fda_event(ticker, within_days=10))

    news_verdict = text(row.get("news_verdict")).upper()
    news_safety = text(row.get("news_safety_status")).upper()
    news_danger = bool(news_verdict == "DANGER" or news_safety in {"DANGER", "UNSAFE"})

    price = num(row.get("price"), 0.0)
    volume = num(row.get("volume") or row.get("averageVolume"), 0.0)
    market_cap = num(row.get("marketCap") or row.get("market_cap"), 0.0)
    below_liquidity_floor = bool(price < 5.0 or volume < 100_000 or (0 < market_cap < 100_000_000))

    flags = {
        "earnings_within_7d": earnings_within_7d,
        "fda_event_pending": fda_event_pending,
        "news_danger": news_danger,
        "below_liquidity_floor": below_liquidity_floor,
    }
    passed_all_gates = not any(flags.values())
    return {**flags, "passed_all_gates": passed_all_gates}


def flatten_record(row: dict[str, Any], tq: dict[str, Any], safety: dict[str, Any], is_finalist: bool) -> dict[str, Any]:
    symbol = text(row.get("symbol") or row.get("ticker")).upper()
    momentum = row.get("momentum") or {}
    if not isinstance(momentum, dict):
        momentum = {}

    danelfin = get_danelfin_score(symbol) or {}

    record: dict[str, Any] = {
        # Identity
        "date": row.get("date") or datetime.now(timezone.utc).date().isoformat(),
        "ticker": symbol,
        "company_name": row.get("companyName") or row.get("name") or row.get("company_name"),
        "sector": row.get("sector"),
        "market_cap": num(row.get("marketCap") or row.get("market_cap"), None),
        "price": num(row.get("price"), None),

        # Technical
        "setup_score": num(row.get("setupQualityScore") or row.get("setup_score"), None),
        "setup_score_raw": num(row.get("raw_score") or row.get("setup_score_raw"), None),
        "rvol": num(row.get("volumeRatio") or row.get("rvol"), None),
        "proximity_52wk": num(row.get("proximity_52wk"), None),
        "pct_from_52wk_high": num(row.get("pct_from_52wk_high"), None),
        "is_new_52wk_high": bool(row.get("is_new_52wk_high")),
        "adx": num(row.get("adx") or momentum.get("adx14"), None),
        "adx_bullish": bool(row.get("adx_bullish")),
        "setup_type": row.get("setup") or row.get("setupType") or row.get("setup_type"),
        "vol_trend_ratio": num(row.get("vol_trend_ratio"), None),
        "vwma_pct": num(row.get("vwma_pct") or row.get("distanceFromVWAP") or row.get("distanceFromVWMA"), None),
        "rs_vs_spy": num(row.get("rsVsSpy") or row.get("rs_vs_spy"), None),
        "atr_pct": num(row.get("atrPct") or row.get("atr_pct"), None),
        "ema_stack_bullish": bool(momentum.get("bullStack") or row.get("ema_stack_bullish")),
        "pullback_score": num(row.get("pullback_score"), None),

        # Positioning
        "dark_pool_pct": num(row.get("dark_pool_pct"), None),
        "dark_pool_signal": row.get("dark_pool_signal"),
        "dark_pool_source": row.get("dark_pool_source", "proxy"),
        "insider_buy_signal": row.get("insider_buy_signal"),
        "insider_buy_value": num(row.get("insider_buy_value"), None),
        "is_cluster_buy": bool(row.get("is_cluster_buy")),
        "iv_rank": num(row.get("iv_rank") or row.get("iv_rank_proxy"), None),

        # Catalyst
        "news_verdict": row.get("news_verdict"),
        "congress_signal": row.get("congress_signal"),
        "pead_score": num(row.get("pead_score"), None),
        "earnings_surprise_pct": num(row.get("earnings_surprise_pct"), None),
        "estimate_revision": row.get("estimate_revision"),
        "analyst_rec_direction": row.get("analyst_rec_direction"),
        "danelfin_aiscore": num(danelfin.get("ai_score"), None),
        "danelfin_low_risk": num(danelfin.get("low_risk"), None),

        # Social
        "social_sentiment": row.get("social_sentiment", "NO_DATA"),
        "social_score": num(row.get("social_score"), None),
        "stocktwits_bull_pct": num(row.get("stocktwits_bull_pct"), None),

        # Options
        "put_call_ratio": num(row.get("put_call_ratio"), None),
        "options_verdict": row.get("options_verdict"),
        "options_provider_used": row.get("options_provider_used"),

        # Safety flags
        **safety,

        # Finalist
        "is_finalist": is_finalist,
        "confluence_score": num(tq.get("trade_quality_score"), None),

        # Full trade quality fields (for Signal Lab queries)
        "core_setup_score": num(tq.get("core_setup_score"), None),
        "confirmation_score": num(tq.get("confirmation_score"), None),
        "risk_score": num(tq.get("risk_score"), None),
        "trade_quality_score": num(tq.get("trade_quality_score"), None),
        "trade_quality_label": tq.get("trade_quality_label"),
        "trade_readiness_tier": tq.get("trade_readiness_tier"),
        "data_quality_score": num(tq.get("data_quality_score"), None),
        "family_scores": tq.get("family_scores"),
        "families_firing": tq.get("families_firing"),
    }

    # Remove explicit None values for compactness where appropriate
    return {k: v for k, v in record.items() if v is not None or k in safety}


def main() -> int:
    run_date = datetime.now(timezone.utc).date().isoformat()
    market_regime = os.environ.get("SYSTEM2_REGIME", "")

    scored = load_json(SCORED_PATH)
    if not isinstance(scored, list):
        print(f"ERROR: {SCORED_PATH} not found or not a list")
        return 1

    stage7 = load_json(STAGE7_PATH) or []
    if not isinstance(stage7, list):
        stage7 = []
    finalist_tickers = {
        text(r.get("symbol") or r.get("ticker")).upper()
        for r in stage7
        if isinstance(r, dict)
    }

    social_overlay = load_social_overlay()

    stocks: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in scored:
        if not isinstance(row, dict):
            continue
        symbol = text(row.get("symbol") or row.get("ticker")).upper()
        if not symbol:
            continue

        # Merge fresh social sentiment if available
        if symbol in social_overlay:
            overlay = social_overlay[symbol]
            row["social_sentiment"] = overlay.get("social_sentiment", "NO_DATA")
            row["social_score"] = overlay.get("score", overlay.get("social_score", 0))
            row["stocktwits_bull_pct"] = overlay.get("stocktwits_bull_pct")

        try:
            tq = compute_trade_quality(dict(row), market_regime)
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")
            continue

        # compute_trade_quality mutates the row copy with enrichment fields
        merged = {**row, **tq}
        safety = compute_safety_flags(merged)
        record = flatten_record(merged, tq, safety, symbol in finalist_tickers)
        stocks.append(record)

    payload = {
        "date": run_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pool_count": len(stocks),
        "finalist_count": sum(1 for s in stocks if s.get("is_finalist")),
        "stocks": stocks,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(OUTPUT_PATH)

    metadata = {
        "date": run_date,
        "generated_at": payload["generated_at"],
        "pool_count": len(stocks),
        "finalist_count": payload["finalist_count"],
        "scored_file": str(SCORED_PATH),
        "stage7_file": str(STAGE7_PATH),
        "errors": errors[:20],
    }
    METADATA_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
