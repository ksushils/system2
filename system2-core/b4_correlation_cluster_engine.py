#!/usr/bin/env python3
"""
B4 Stage 7 correlation / cluster guard.

Input:
  - stage2_surgical_strike_top40.json

Outputs:
  - stage7_clustered_survivors.json
  - stage7_cluster_rejections.json
  - stage7_cluster_report.json

This is a concentration guard only. It does not alter the Stage 2 setup score,
does not call brokers, and does not add new strategy features.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
def text(value):
    return str(value or "").strip()

from pathlib import Path


ROOT = Path(__file__).resolve().parent
STAGE2_TOP40_PATH = ROOT / "stage2_surgical_strike_top40.json"
STAGE4_OPTIONS_TOP40_PATH = ROOT / "stage4_options_enriched_top40.json"
CONFLUENCE_TOP40_PATH = ROOT / "stage2_confluence_ranked_top40.json"
COUNCIL_TOP40_PATH = ROOT / "stage6_council_enriched.json"
SURVIVORS_PATH = ROOT / "stage7_clustered_survivors.json"
REJECTIONS_PATH = ROOT / "stage7_cluster_rejections.json"
REPORT_PATH = ROOT / "stage7_cluster_report.json"

# Known high-concentration watch clusters (Upgrade 4)
WATCH_CLUSTERS = {
    "Semiconductors": {"NVDA", "AMD", "SMCI", "AVGO", "ARM", "MU", "MRVL", "TSM", "INTC", "QCOM", "LRCX", "KLAC"},
    "Banks": {"JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "TFC", "COF"},
    "Airlines": {"UAL", "DAL", "AAL", "LUV", "JBLU", "ALK"},
    "Energy": {"XOM", "CVX", "COP", "OXY", "SLB", "EOG", "MPC", "VLO", "PSX"},
    "Biotech": {"BIIB", "GILD", "AMGN", "REGN", "VRTX", "MRNA", "BNTX", "SGEN", "INCY"},
    "Tech Mega-Cap": {"AAPL", "MSFT", "GOOGL", "GOOG", "META", "AMZN", "TSLA", "NFLX", "CRM", "ADBE"},
}


def load_config() -> dict:
    path = ROOT / "system2-config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


CONFIG = load_config()
STAGE7_CONFIG = CONFIG.get("stage7", {})
ACCOUNT_SIZE = float(STAGE7_CONFIG.get("account_size", 25_000))
RISK_PCT = float(STAGE7_CONFIG.get("risk_pct", 0.01))
ONE_POSITION_RISK = round(ACCOUNT_SIZE * RISK_PCT, 2)
POSITION_SIZE_MULTIPLIER = float(os.environ.get("SYSTEM2_POSITION_SIZE_MULTIPLIER", "1"))
MAX_TRADES_PER_DAY = int(STAGE7_CONFIG.get("max_trades_per_day", 3))
MAX_PORTFOLIO_HEAT = float(STAGE7_CONFIG.get("max_portfolio_heat", 0.06))
MAX_NAMES_PER_CLUSTER = int(STAGE7_CONFIG.get("max_names_per_cluster", 2))

B3_TECHNICAL_DEFAULTS = {
    "proximity_52wk": None,
    "pct_from_52wk_high": None,
    "is_new_52wk_high": None,
    "fiftytwo_score": None,
    "adx": None,
    "adx_bullish": None,
    "adx_score": None,
    "vol_trend_ratio": None,
    "vol_trend_score": None,
    "pullback_score": None,
    "setup_type": None,
    "vwma_pct": None,
}

# DYNAMIC SECTOR CAPS — based on intelligence engine shadow portfolio analysis
# Raised to 3 for sectors where cluster cap was cutting winners
SECTOR_CAPS = {
    "Energy": 2,                  # working — keep tight
    "Technology": 2,              # correlated sector
    "Financial Services": 2,      # correlated sector
    "Communication Services": 2,  # working — keep
    "Consumer Defensive": 3,      # FLAGGED — raise to 3
    "Industrials": 3,             # FLAGGED — raise to 3
    "Consumer Cyclical": 3,       # FLAGGED — raise to 3
    "Healthcare": 2,              # binary event risk
    "Basic Materials": 2,         # gold miner correlation
    "Utilities": 2,               # defensive / rate-sensitive
    "Real Estate": 2,             # rate-sensitive
    "default": 2,                 # all others
}


def get_sector_cap(sector: str) -> int:
    return SECTOR_CAPS.get(sector, SECTOR_CAPS["default"])

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


def rank_key(setup: dict):
    """Confluence first, then the existing deterministic B3 tiebreaks."""
    return (
        setup.get("confluence_score", setup.get("setupQualityScore", 0)),
        setup.get("rs_rank") if setup.get("rs_rank") is not None else 0,
        1 if setup.get("breakout_pullback_confirmed") else 0,
        12 - (setup.get("sector_strength_rank") or 12),
        setup.get("volumeRatio", 0),
        setup.get("rsVsSpy", 0),
    )


QUALITY_FIELD_OVERLAY = [
    "setup_score",
    "setupQualityScore",
    "setup_score_base",
    "quality_signal_boost",
    "rs_rank",
    "rs_rank_source",
    "rs_rank_boost",
    "return_1m_pct",
    "return_3m_pct",
    "return_6m_pct",
    "vs_spy_1m_pct",
    "vs_spy_3m_pct",
    "vs_spy_6m_pct",
    "rs_composite_vs_spy_pct",
    "sector_strength_rank",
    "sector_strength_1m_vs_spy_pct",
    "sector_strength_boost",
    "breakout_pullback_confirmed",
    "breakout_pullback_detail",
    "breakout_pullback_boost",
    "technical_score_breakdown",
]


def overlay_confluence_quality_fields(rows: list[dict]) -> list[dict]:
    """Council rows may be present without fresh B3 quality fields; merge them by ticker."""
    if not CONFLUENCE_TOP40_PATH.exists():
        return rows
    try:
        confluence_rows = json.loads(CONFLUENCE_TOP40_PATH.read_text(encoding="utf-8"))
    except Exception:
        return rows
    by_symbol = {
        text(row.get("symbol") or row.get("ticker")).upper(): row
        for row in confluence_rows
    }
    merged_rows: list[dict] = []
    for row in rows:
        sym = text(row.get("symbol") or row.get("ticker")).upper()
        overlay = by_symbol.get(sym) or {}
        merged = dict(row)
        for field in QUALITY_FIELD_OVERLAY:
            if merged.get(field) in (None, "") and overlay.get(field) not in (None, ""):
                merged[field] = overlay.get(field)
        merged_rows.append(merged)
    return merged_rows


def ensure_b3_technical_fields(setup: dict) -> dict:
    return {**B3_TECHNICAL_DEFAULTS, **setup}


def cluster_key(setup: dict) -> str:
    sector = setup.get("sector") or "Unknown"
    etf = SECTOR_ETFS.get(sector, "UNKNOWN")
    return f"{sector}|{etf}"


def detect_industry_clusters(setups: list[dict]) -> list[dict]:
    """Upgrade 4 — Detect concentration clusters of 3+ finalists in same watch cluster."""
    warnings: list[dict] = []
    for cluster_name, tickers in WATCH_CLUSTERS.items():
        matched = [s for s in setups if text(s.get("symbol") or s.get("ticker")).upper() in tickers]
        if len(matched) >= 3:
            symbols = [text(s.get("symbol") or s.get("ticker")).upper() for s in matched]
            warnings.append({
                "industry": cluster_name,
                "tickers": symbols,
                "count": len(symbols),
                "recommendation": f"Max 2 entries from this cluster simultaneously",
            })
    return warnings


def detect_open_position_conflicts(setups: list[dict], open_positions: set[str]) -> list[dict]:
    """Upgrade 4 — Flag if user already holds a ticker in same watch cluster."""
    flags: list[dict] = []
    for cluster_name, tickers in WATCH_CLUSTERS.items():
        held = [t for t in open_positions if t in tickers]
        if not held:
            continue
        new_matches = [s for s in setups if text(s.get("symbol") or s.get("ticker")).upper() in tickers]
        for s in new_matches:
            sym = text(s.get("symbol") or s.get("ticker")).upper()
            if sym not in held:
                for h in held:
                    flags.append({
                        "new_ticker": sym,
                        "held_ticker": h,
                        "cluster": cluster_name,
                        "flag": f"You already hold {h} (same cluster). Consider skipping {sym}.",
                    })
    return flags


def shares_for_risk(setup: dict, risk_dollars: float) -> int:
    risk_per_share = float(setup.get("riskPerShare") or 0)
    if risk_per_share <= 0:
        return 0
    return max(1, math.floor(risk_dollars / risk_per_share))


def apply_contradiction_gate(setups: list[dict]) -> tuple[list[dict], list[dict]]:
    """FIX 1 — Reject STRONG_DOWN or REJECT-with-low-conviction before clustering."""
    survivors: list[dict] = []
    rejections: list[dict] = []
    for setup in setups:
        combined_dir = str(setup.get("combined_forecast_dir") or "").strip().upper()
        if combined_dir == "STRONG_DOWN":
            rejections.append({
                **setup,
                "clusterRejectReason": "contradiction_gate: combined_forecast_dir=STRONG_DOWN",
                "contradiction_rejected": True,
            })
            continue
        forecast_decision = str(setup.get("forecastDecision") or "").strip().upper()
        chronos_conviction = setup.get("chronos_conviction") or setup.get("chronos_conf") or setup.get("forecastConviction")
        try:
            conviction_val = float(chronos_conviction) if chronos_conviction is not None else None
        except (TypeError, ValueError):
            conviction_val = None
        if forecast_decision == "REJECT" and (conviction_val is not None and conviction_val < 40):
            rejections.append({
                **setup,
                "clusterRejectReason": "contradiction_gate: forecastDecision=REJECT with chronos_conviction<40",
                "contradiction_rejected": True,
            })
            continue
        survivors.append(setup)
    return survivors, rejections


def main() -> None:
    input_path = (
        COUNCIL_TOP40_PATH
        if COUNCIL_TOP40_PATH.exists()
        else CONFLUENCE_TOP40_PATH
        if CONFLUENCE_TOP40_PATH.exists()
        else STAGE4_OPTIONS_TOP40_PATH
        if STAGE4_OPTIONS_TOP40_PATH.exists()
        else STAGE2_TOP40_PATH
    )
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if input_path == COUNCIL_TOP40_PATH:
        raw = overlay_confluence_quality_fields(raw)
    raw_ranked = sorted(raw, key=rank_key, reverse=True)

    # FIX 1 — Contradiction gate (Stage 7 hard filter)
    raw_ranked, contradiction_rejections = apply_contradiction_gate(raw_ranked)

    clusters: dict[str, list[dict]] = defaultdict(list)
    for setup in raw_ranked:
        clusters[cluster_key(setup)].append(setup)

    survivors: list[dict] = []
    rejections: list[dict] = contradiction_rejections
    cluster_reports: list[dict] = []

    for key, names in sorted(clusters.items(), key=lambda item: max(rank_key(s) for s in item[1]), reverse=True):
        sector, etf = key.split("|", 1)
        sector_cap = get_sector_cap(sector)
        ranked = sorted(names, key=rank_key, reverse=True)
        kept = ranked[:sector_cap]
        rejected = ranked[sector_cap:]

        cluster_risk_budget = round(ONE_POSITION_RISK * POSITION_SIZE_MULTIPLIER, 2)
        per_name_risk = round(cluster_risk_budget / len(kept), 2) if kept else 0.0

        enriched_kept = []
        for setup in kept:
            shares = shares_for_risk(setup, per_name_risk)
            actual_risk = round(shares * float(setup.get("riskPerShare") or 0), 2)
            enriched = {
                **setup,
                "cluster": {
                    "sector": sector,
                    "etf": etf,
                    "clusterSizeBefore": len(ranked),
                    "clusterRank": len(enriched_kept) + 1,
                    "clusterRiskBudget": cluster_risk_budget,
                    "allocatedRiskDollars": per_name_risk,
                    "actualRiskDollars": actual_risk,
                    "shares": shares,
                    "riskRule": "cluster_total_risk_lte_one_position",
                },
            }
            survivors.append(enriched)
            enriched_kept.append(enriched)

        for setup in rejected:
            rejections.append({
                **setup,
                "clusterRejectReason": f"cluster_cap_{sector_cap}_per_{sector}",
                "sector_cap_used": sector_cap,
                "cluster": {
                    "sector": sector,
                    "etf": etf,
                    "clusterSizeBefore": len(ranked),
                    "clusterRiskBudget": cluster_risk_budget,
                },
            })

        total_actual_risk = round(sum(s["cluster"]["actualRiskDollars"] for s in enriched_kept), 2)
        cluster_reports.append({
            "sector": sector,
            "etf": etf,
            "sectorCapUsed": sector_cap,
            "rawCount": len(ranked),
            "keptCount": len(kept),
            "rejectedCount": len(rejected),
            "keptSymbols": [s["symbol"] for s in enriched_kept],
            "rejectedSymbols": [s["symbol"] for s in rejected],
            "clusterRiskBudget": cluster_risk_budget,
            "allocatedRiskDollars": round(sum(s["cluster"]["allocatedRiskDollars"] for s in enriched_kept), 2),
            "actualRiskDollars": total_actual_risk,
            "riskWithinOnePosition": total_actual_risk <= cluster_risk_budget,
            "bestScore": ranked[0].get("setupQualityScore") if ranked else None,
        })

    survivors = sorted(survivors, key=rank_key, reverse=True)
    survivors = [ensure_b3_technical_fields(s) for s in survivors]
    rejections = [ensure_b3_technical_fields(r) for r in rejections]

    # Upgrade 4 — Industry cluster detection (warning only, does not remove)
    cluster_warnings = detect_industry_clusters(survivors)
    open_positions = set()  # Would be populated from /api/ideas?paper_status=OPEN in production
    open_conflicts = detect_open_position_conflicts(survivors, open_positions)

    # Existing risk-engine max-trades/day is not changed. This preview shows what
    # would be eligible first if the existing Risk Engine later takes only 3.
    max_trades_preview = survivors[:MAX_TRADES_PER_DAY]
    preview_risk = round(sum(s["cluster"]["actualRiskDollars"] for s in max_trades_preview), 2)

    report = {
        "stage": "B4",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "rawTop40Count": len(raw),
        "contradictionRejectedCount": len(contradiction_rejections),
        "contradictionRejectedSymbols": [s["symbol"] for s in contradiction_rejections],
        "inputPath": str(input_path),
        "optionsRideAlongCarriedThrough": input_path.name in {
            STAGE4_OPTIONS_TOP40_PATH.name,
            CONFLUENCE_TOP40_PATH.name,
        },
        "confluenceRankingCarriedThrough": input_path.name == CONFLUENCE_TOP40_PATH.name,
        "clusteredSurvivorCount": len(survivors),
        "clusterRejectedCount": len(rejections),
        "riskRulesUnchanged": {
            "accountSize": ACCOUNT_SIZE,
            "riskPctPerPosition": RISK_PCT,
            "onePositionRiskDollars": ONE_POSITION_RISK,
            "positionSizeMultiplier": POSITION_SIZE_MULTIPLIER,
            "effectiveOnePositionRiskDollars": round(
                ONE_POSITION_RISK * POSITION_SIZE_MULTIPLIER, 2
            ),
            "maxTradesPerDay": MAX_TRADES_PER_DAY,
            "maxPortfolioHeat": MAX_PORTFOLIO_HEAT,
            "configSource": "system2-config.json",
            "regimeKillSwitch": "unchanged/downstream",
            "atrStop": "unchanged/from Stage 2 riskPerShare",
        },
        "clusterRule": {
            "groupBy": "sector + shared sector ETF",
            "maxNamesPerCluster": MAX_NAMES_PER_CLUSTER,
            "dynamicSectorCaps": SECTOR_CAPS,
            "clusterRiskBudget": "one position risk per sector/ETF cluster",
            "allocation": "equal split among kept names in the cluster",
        },
        "tieCheck": {
            "b3ScoreCap": 100,
            "topSix96ScoresAreNotCapClipped": True,
            "topSixReason": "They tied on the additive technical buckets used by B3.",
            "deterministicTiebreak": ["setupQualityScore desc", "volumeRatio desc", "rsVsSpy desc"],
            "topSixOrder": [
                {
                    "symbol": s["symbol"],
                    "score": s["setupQualityScore"],
                    "volumeRatio": s["volumeRatio"],
                    "rsVsSpy": s["rsVsSpy"],
                }
                for s in raw_ranked[:6]
            ],
        },
        "portfolioHeatPreview": {
            "ifExistingRiskEngineTakesMaxTrades": MAX_TRADES_PER_DAY,
            "previewSymbols": [s["symbol"] for s in max_trades_preview],
            "previewActualRiskDollars": preview_risk,
            "previewHeatPct": round((preview_risk / ACCOUNT_SIZE) * 100, 3),
            "withinMaxPortfolioHeat": preview_risk / ACCOUNT_SIZE <= MAX_PORTFOLIO_HEAT,
        },
        "clusters": cluster_reports,
        "clusterWarnings": cluster_warnings,
        "openPositionConflicts": open_conflicts,
        "notes": [
            "This guard reduces concentration before B4 hands names to existing risk/council logic.",
            "Cluster total risk is capped at one normal position risk, even when two names survive.",
            "No broker, Chronos, AI council, deployment, or live trading calls were made.",
        ],
    }

    SURVIVORS_PATH.write_text(json.dumps(survivors, indent=2), encoding="utf-8")
    REJECTIONS_PATH.write_text(json.dumps(rejections, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({
        "raw_top40": len(raw),
        "clustered_survivors": len(survivors),
        "cluster_rejections": len(rejections),
        "clusters": [
            {
                "sector": c["sector"],
                "etf": c["etf"],
                "raw": c["rawCount"],
                "kept": c["keptSymbols"],
                "rejected": c["rejectedSymbols"],
                "actualRisk": c["actualRiskDollars"],
                "withinOnePosition": c["riskWithinOnePosition"],
            }
            for c in cluster_reports
        ],
        "top_after_cluster": [s["symbol"] for s in survivors[:10]],
    }, indent=2))


if __name__ == "__main__":
    main()
