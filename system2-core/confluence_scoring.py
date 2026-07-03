#!/usr/bin/env python3
"""
Re-rank the fixed Stage 2 top 40 after all enrichment.
v2: 3-category trade quality scoring replaces flat confluence bonus system.
confluence_score is kept for backwards compatibility = trade_quality_score.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scoring_engine import (
    compute_trade_quality,
    compute_data_quality_score,
    generate_bear_case_points,
)
from stage_alignment_guard import per_idea_enrichment_check


ROOT = Path(__file__).resolve().parent
STAGE2_PATH = ROOT / "stage2_surgical_strike_top40.json"
ENRICHED_PATH = ROOT / "stage3_options_enriched_top40.json"
COUNCIL_PATH = ROOT / "stage6_council_enriched.json"
FORECAST_PATH = ROOT / "stage5_combined_forecast_top40.json"
OUTPUT_PATH = ROOT / "stage2_confluence_ranked_top40.json"
METADATA_PATH = ROOT / "stage2_confluence_metadata.json"


def text(value: Any) -> str:
    return str(value or "").strip()


def number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def symbol(row: dict[str, Any]) -> str:
    return text(row.get("symbol") or row.get("ticker")).upper()


def load_config() -> dict:
    path = ROOT / "system2-config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_best_enriched() -> list[dict[str, Any]]:
    """Merge all available enrichment files. Later files override earlier ones."""
    paths = [
        ENRICHED_PATH,      # base: options + chronos
        FORECAST_PATH,      # overlay: combined forecast
        COUNCIL_PATH,       # overlay: council verdicts
    ]
    merged_by_symbol: dict[str, dict] = {}
    for p in paths:
        if not p.exists():
            continue
        rows = json.loads(p.read_text(encoding="utf-8-sig"))
        for row in rows:
            sym = symbol(row)
            if sym in merged_by_symbol:
                merged_by_symbol[sym].update(row)
            else:
                merged_by_symbol[sym] = dict(row)
    return list(merged_by_symbol.values())


def merge_rows(fixed: list[dict], enriched: list[dict]) -> list[dict]:
    """Merge fixed Stage 2 top-40 with enriched data, preserving membership."""
    enriched_by_symbol = {symbol(row): row for row in enriched}
    return [
        {**row, **enriched_by_symbol.get(symbol(row), {})}
        for row in fixed
    ]


def calculate(row: dict[str, Any]) -> dict[str, Any]:
    """Compute all quality scores for a single idea."""
    setup_score = number(row.get("setupQualityScore") or row.get("setup_score")) or 0

    # Belt-and-braces: check if this specific idea is missing enrichment
    enrichment_check = per_idea_enrichment_check(row)
    if enrichment_check.get("partial_enrichment"):
        # Log the note — bonuses are naturally suppressed by missing data
        # (compute_options_conf and compute_chronos_conf return low scores
        # when options_provider_used / forecast fields are absent)
        note = enrichment_check.get("enrichment_note")
        if note:
            print(f"[ALIGNMENT] {note}")
        row = {**row, **enrichment_check}

    # 3-category trade quality scoring
    market_regime = os.environ.get("SYSTEM2_REGIME", "")
    tq = compute_trade_quality(row, market_regime)

    # Data quality
    dq = compute_data_quality_score(row)

    # Bear case
    bear = generate_bear_case_points(row)

    # Backwards compatibility: confluence_score = trade_quality_score
    confluence_score = tq["trade_quality_score"]

    # Configurable finalist threshold (raised to 50 based on historical signal
    # validation: confluence ≥70 → +0.25R avg, 60% WR)
    config = load_config()
    finalist_threshold = config.get("trade_quality", {}).get("finalist_threshold", 50)

    # Build sources list for backwards compatibility
    sources = set()
    all_src = row.get("all_sources")
    if isinstance(all_src, list):
        sources.update(text(s).lower() for s in all_src if text(s))
    sources.add(text(row.get("primary_source") or row.get("source") or "scanner").lower())
    source_count = len(sources)

    return {
        **row,
        "setup_score": setup_score,
        # v2 3-category scores
        "core_setup_score": tq["core_setup_score"],
        "core_setup_breakdown": tq["core_setup_breakdown"],
        "confirmation_score": tq["confirmation_score"],
        "confirmation_breakdown": tq["confirmation_breakdown"],
        "risk_score": tq["risk_score"],
        "risk_breakdown": tq["risk_breakdown"],
        "trade_quality_score": tq["trade_quality_score"],
        "trade_quality_label": tq["trade_quality_label"],
        "trade_quality_finalist": tq["trade_quality_score"] >= finalist_threshold,
        "trade_readiness_tier": tq.get("trade_readiness_tier"),
        "trade_readiness": tq.get("trade_readiness"),
        "evidence_scoreboard": tq.get("evidence_scoreboard"),
        "trader_thesis_card": tq.get("trader_thesis_card"),
        "family_scores": tq.get("family_scores", {}),
        "families_firing": tq.get("families_firing"),
        # Regime-adaptive scoring metadata
        "market_regime": tq.get("market_regime"),
        "market_regime_detected": tq.get("market_regime_detected"),
        "regime_caps_applied": tq.get("regime_caps_applied"),
        "regime_weights_applied": tq.get("regime_weights_applied"),
        # Data quality
        "data_quality_score": dq["data_quality_score"],
        "data_quality_label": dq["data_quality_label"],
        "data_quality_checks": dq["data_quality_checks"],
        # Bear case
        "bear_case_points": bear,
        # Backwards compatibility
        "confluence_score": confluence_score,
        "all_sources": sorted(sources),
        "source_count": source_count,
    }


def rank_key(row: dict[str, Any]) -> tuple[float, float, float, float, float]:
    return (
        number(row.get("trade_quality_score")) or float("-inf"),
        number(row.get("rs_rank")) or 0,
        1.0 if row.get("breakout_pullback_confirmed") else 0.0,
        (12 - (number(row.get("sector_strength_rank")) or 12)),
        number(row.get("volumeRatio")) or 0,
    )


def main() -> None:
    fixed_top40 = json.loads(STAGE2_PATH.read_text(encoding="utf-8-sig"))
    enriched = load_best_enriched()
    rows = merge_rows(fixed_top40, enriched)

    # Calculate scores for all rows
    rows = [calculate(row) for row in rows]

    # Separate finalists (score >= 55) from watchlist
    finalists = [row for row in rows if row.get("trade_quality_finalist")]
    watchlist = [row for row in rows if not row.get("trade_quality_finalist")]

    # Rank finalists first, then watchlist
    finalists.sort(key=rank_key, reverse=True)
    watchlist.sort(key=rank_key, reverse=True)
    ranked = finalists + watchlist

    # Boundary guard
    fixed_symbols = {symbol(row) for row in fixed_top40}
    ranked_symbols = {symbol(row) for row in ranked}
    if len(ranked) != len(fixed_top40) or fixed_symbols != ranked_symbols:
        raise RuntimeError(
            f"Confluence boundary violation: fixed Stage 2 top-{len(fixed_top40)} membership changed "
            f"(output={len(ranked)}, fixed={len(fixed_top40)}, symbols_match={fixed_symbols == ranked_symbols})"
        )

    for rank, row in enumerate(ranked, start=1):
        row["confluence_rank"] = rank

    # Count by label
    label_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    for row in ranked:
        label = row.get("trade_quality_label", "UNKNOWN")
        label_counts[label] = label_counts.get(label, 0) + 1
        tier = row.get("trade_readiness_tier", "UNKNOWN")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    metadata = {
        "stage": "CONFLUENCE_V2",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "mode": "paper",
        "inputCount": len(fixed_top40),
        "outputCount": len(ranked),
        "finalistCount": len(finalists),
        "watchlistCount": len(watchlist),
        "membershipUnchanged": fixed_symbols == ranked_symbols,
        "labelDistribution": label_counts,
        "tierDistribution": tier_counts,
        "top10": [
            {
                "rank": row["confluence_rank"],
                "symbol": symbol(row),
                "setup_score": row["setup_score"],
                "core_setup_score": row["core_setup_score"],
                "confirmation_score": row["confirmation_score"],
                "risk_score": row["risk_score"],
                "trade_quality_score": row["trade_quality_score"],
                "trade_quality_label": row["trade_quality_label"],
                "rs_rank": row.get("rs_rank"),
                "sector_strength_rank": row.get("sector_strength_rank"),
                "breakout_pullback_confirmed": row.get("breakout_pullback_confirmed"),
                "trade_readiness_tier": row.get("trade_readiness_tier"),
                "data_quality_score": row["data_quality_score"],
            }
            for row in ranked[:10]
        ],
        "boundaryRule": "Re-rank the immutable Stage 2 top 40 only.",
        "paperOnly": True,
    }
    OUTPUT_PATH.write_text(json.dumps(ranked, indent=2), encoding="utf-8")
    METADATA_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
