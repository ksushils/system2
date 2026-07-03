#!/usr/bin/env python3
"""
Upgrade 7 — FMP News + Analyst Signals enrichment.
Free FMP endpoints. Adds news risk, analyst momentum,
and earnings consistency to each Stage 2 top-40 ticker.

Runs as a pipeline stage after Stage 2 technical scoring,
before options enrichment.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import fmp_api


ROOT = Path(__file__).resolve().parent
INPUT_PATH = ROOT / "stage2_surgical_strike_top40.json"
OUTPUT_PATH = ROOT / "stage2_surgical_strike_top40.json"
METADATA_PATH = ROOT / "fmp_news_analyst_metadata.json"

# News keyword flags (case-insensitive)
NEGATIVE_KEYWORDS = [
    "investigation", "lawsuit", "fraud", "recall", "fda rejection",
    "downgrade", "miss", "warning", "bankruptcy", "layoff", "sec",
    "criminal", "loss", "cut", "restated", "restatement", "probe",
    "subpoena", "settlement", "penalty", "fine", "breach",
]
POSITIVE_KEYWORDS = [
    "approval", "upgrade", "beat", "expansion", "contract",
    "partnership", "buyback", "dividend", "acquisition target",
    "fda approval", "clearance", "launch", "milestone",
]

MAX_WORKERS = 4
DAYS_LOOKBACK_NEWS = 2
DAYS_LOOKBACK_ANALYST = 7


def text(value: Any) -> str:
    return str(value or "").strip()


def fmp_key() -> str:
    return os.environ.get("FMP_API_KEY", "").strip()


def fmp_get(path: str, params: dict[str, str] | None = None) -> list | dict:
    """Thin wrapper around fmp_api.get for error resilience."""
    try:
        return fmp_api.get(path, params=params or {}, api_key=fmp_key())
    except Exception as e:
        return {"error": str(e)}


def analyze_news(items: list[dict]) -> dict[str, Any]:
    """Analyze FMP stock-news items for risk flags and sentiment."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK_NEWS)
    recent = []
    for item in items:
        pub = item.get("publishedDate") or item.get("date") or ""
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if dt >= cutoff:
                recent.append(item)
        except Exception:
            continue

    negative_hits = 0
    positive_hits = 0
    flagged_headlines: list[str] = []
    first_headline = ""

    for item in recent:
        headline = text(item.get("title")).lower()
        if not first_headline:
            first_headline = text(item.get("title"))
        for kw in NEGATIVE_KEYWORDS:
            if kw.lower() in headline:
                negative_hits += 1
                flagged_headlines.append(text(item.get("title")))
                break
        for kw in POSITIVE_KEYWORDS:
            if kw.lower() in headline:
                positive_hits += 1
                break

    news_flag_count = negative_hits
    recent_negative_news = negative_hits > 0

    if news_flag_count >= 3 or (recent_negative_news and news_flag_count >= 2):
        news_risk = "HIGH"
    elif news_flag_count >= 1:
        news_risk = "MEDIUM"
    else:
        news_risk = "LOW"

    if positive_hits > negative_hits:
        sentiment = "POSITIVE"
    elif negative_hits > 0:
        sentiment = "NEGATIVE"
    else:
        sentiment = "NEUTRAL"

    return {
        "news_risk": news_risk,
        "news_sentiment": sentiment,
        "news_flag_count": news_flag_count,
        "recent_negative_news": recent_negative_news,
        "news_summary": first_headline,
        "news_items_checked": len(recent),
        "news_hard_reject": news_flag_count >= 3 or (recent_negative_news and news_flag_count >= 2),
    }


def analyze_analyst(items: list[dict]) -> dict[str, Any]:
    """Analyze FMP upgrades-downgrades for momentum signal."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK_ANALYST)
    recent = []
    for item in items:
        pub = item.get("publishedDate") or item.get("date") or ""
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if dt >= cutoff:
                recent.append(item)
        except Exception:
            continue

    upgrades = 0
    downgrades = 0
    pt_raised = False
    pt_cut = False

    for item in recent:
        rating = text(item.get("newGrade")).lower()
        old = text(item.get("previousGrade")).lower()
        # Simple heuristic: if new grade is better than old
        upgrade_terms = {"buy", "strong buy", "overweight", "outperform"}
        downgrade_terms = {"sell", "strong sell", "underweight", "underperform"}
        if any(t in rating for t in upgrade_terms):
            upgrades += 1
        if any(t in rating for t in downgrade_terms):
            downgrades += 1
        # Price target change
        new_pt = number(item.get("priceTarget"))
        old_pt = number(item.get("previousPriceTarget"))
        if new_pt and old_pt:
            if new_pt > old_pt:
                pt_raised = True
            elif new_pt < old_pt:
                pt_cut = True

    if upgrades >= 2 and downgrades == 0:
        trend = "IMPROVING"
    elif downgrades >= 2 and upgrades == 0:
        trend = "DETERIORATING"
    else:
        trend = "STABLE"

    if upgrades > downgrades:
        signal = "POSITIVE"
    elif downgrades > upgrades:
        signal = "NEGATIVE"
    else:
        signal = "NEUTRAL"

    return {
        "analyst_upgrades_7d": upgrades,
        "analyst_downgrades_7d": downgrades,
        "analyst_pt_raised_7d": pt_raised,
        "analyst_pt_cut_7d": pt_cut,
        "analyst_consensus_trend": trend,
        "analyst_signal": signal,
        "analyst_items_checked": len(recent),
    }


def analyze_earnings(surprises: list[dict]) -> dict[str, Any]:
    """Analyze last 4 quarters of earnings surprises."""
    if not surprises:
        return {
            "earnings_beat_rate": None,
            "consistent_beater": False,
            "earnings_surprises_checked": 0,
        }

    beats = 0
    total = 0
    total_surprise_pct = 0.0

    for item in surprises[:4]:
        actual = number(item.get("actualEarningResult"))
        estimate = number(item.get("estimatedEarning"))
        if actual is not None and estimate is not None and estimate != 0:
            total += 1
            surprise_pct = (actual - estimate) / abs(estimate) * 100
            total_surprise_pct += surprise_pct
            if actual > estimate:
                beats += 1

    beat_rate = beats / total if total > 0 else None
    return {
        "earnings_beat_rate": beat_rate,
        "consistent_beater": beat_rate is not None and beat_rate >= 0.75,
        "earnings_surprises_checked": total,
        "avg_earnings_surprise_pct": round(total_surprise_pct / total, 2) if total > 0 else None,
    }


def enrich_symbol(sym: str) -> dict[str, Any]:
    """Fetch and analyze all three FMP endpoints for one symbol."""
    sym = sym.upper()
    result: dict[str, Any] = {"symbol": sym}

    # A. Stock news
    news_raw = fmp_get(f"stable/stock-news", {"symbol": sym, "limit": "5"})
    if isinstance(news_raw, list):
        result.update(analyze_news(news_raw))
    else:
        result.update({
            "news_risk": "UNKNOWN",
            "news_sentiment": "NEUTRAL",
            "news_flag_count": 0,
            "recent_negative_news": False,
            "news_summary": "",
            "news_items_checked": 0,
            "news_hard_reject": False,
            "news_fetch_error": text(news_raw.get("error")) if isinstance(news_raw, dict) else "unknown",
        })

    # B. Analyst upgrades/downgrades
    analyst_raw = fmp_get(f"stable/upgrades-downgrades", {"symbol": sym})
    if isinstance(analyst_raw, list):
        result.update(analyze_analyst(analyst_raw))
    else:
        result.update({
            "analyst_upgrades_7d": 0,
            "analyst_downgrades_7d": 0,
            "analyst_pt_raised_7d": False,
            "analyst_pt_cut_7d": False,
            "analyst_consensus_trend": "STABLE",
            "analyst_signal": "NEUTRAL",
            "analyst_items_checked": 0,
            "analyst_fetch_error": text(analyst_raw.get("error")) if isinstance(analyst_raw, dict) else "unknown",
        })

    # C. Earnings surprises
    earn_raw = fmp_get(f"stable/earnings-surprises", {"symbol": sym})
    if isinstance(earn_raw, list):
        result.update(analyze_earnings(earn_raw))
    else:
        result.update({
            "earnings_beat_rate": None,
            "consistent_beater": False,
            "earnings_surprises_checked": 0,
            "earnings_fetch_error": text(earn_raw.get("error")) if isinstance(earn_raw, dict) else "unknown",
        })

    return result


def main() -> None:
    rows = json.loads(INPUT_PATH.read_text(encoding="utf-8-sig"))
    symbols = [text(row.get("symbol") or row.get("ticker")).upper() for row in rows]

    enriched_by_symbol: dict[str, dict] = {}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(enrich_symbol, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                enriched_by_symbol[sym] = future.result()
            except Exception as e:
                errors.append(f"{sym}: {e}")
                enriched_by_symbol[sym] = {"symbol": sym, "error": str(e)}

    # Merge enrichment back into rows
    for row in rows:
        sym = text(row.get("symbol") or row.get("ticker")).upper()
        row.update(enriched_by_symbol.get(sym, {}))

    # Metadata
    hard_rejects = sum(1 for r in rows if r.get("news_hard_reject"))
    analyst_positive = sum(1 for r in rows if r.get("analyst_signal") == "POSITIVE")
    analyst_negative = sum(1 for r in rows if r.get("analyst_signal") == "NEGATIVE")
    consistent_beaters = sum(1 for r in rows if r.get("consistent_beater"))

    metadata = {
        "stage": "FMP_NEWS_ANALYST",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "inputCount": len(rows),
        "outputCount": len(rows),
        "hard_rejects": hard_rejects,
        "analyst_positive": analyst_positive,
        "analyst_negative": analyst_negative,
        "consistent_beaters": consistent_beaters,
        "errors": errors,
        "paperOnly": True,
    }

    OUTPUT_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    METADATA_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
