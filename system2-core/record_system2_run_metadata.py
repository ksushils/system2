#!/usr/bin/env python3
"""
Record System2 run metadata and rejections.

Measurement-only. This reads artifacts created by the bare-core runner and
posts metadata/rejection records to the fund-system scoring API. It does not
change finalist selection, call brokers, or place trades.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "http://127.0.0.1:3210/api/system2/run-metadata"


def load_json(name: str, fallback):
    path = ROOT / name
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def post_json(url: str, payload: dict, retries: int = 5, backoff: float = 3.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "system2-run-recorder/1.0"}
    scanner_key = os.environ.get("SCANNER_API_KEY")
    if scanner_key:
        headers["X-Scanner-Key"] = scanner_key
    last_exc = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.URLError as e:
            last_exc = e
            if hasattr(e.reason, "errno") and e.reason.errno == 111:
                print(f"[post_json] Connection refused (attempt {attempt}/{retries}), retrying in {backoff}s...")
                time.sleep(backoff)
                continue
            raise
    print(f"[post_json] All {retries} attempts failed, returning error dict")
    return {"ok": False, "error": str(last_exc)}


def reject_rows_from_stage1(run_date: str, rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if row.get("status") == "PASS":
            continue
        reasons = row.get("rejectReasons") or ["stage1_reject"]
        out.append({
            "date": run_date,
            "ticker": row.get("symbol"),
            "stage_rejected": "stage1",
            "reason": ", ".join(reasons),
            "near_miss": False,
            "price_scoring_eligible": False,
        })
    return out


def reject_rows_from_stage2(run_date: str, scored: list[dict], top40: list[dict]) -> list[dict]:
    top = {row.get("symbol") for row in top40}
    out = []
    for row in scored:
        symbol = row.get("symbol")
        if not symbol or symbol in top:
            continue
        if row.get("status") != "OK":
            reason = row.get("stage2RejectReason") or "technical_reject"
        else:
            reason = "weak_setup_not_top40"
        out.append({
            "date": run_date,
            "ticker": symbol,
            "stage_rejected": "technical",
            "reason": reason,
            "setup_score": row.get("setupQualityScore"),
            "near_miss": False,
            "price_scoring_eligible": False,
        })
    return out


def reject_rows_from_stage7(run_date: str, rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.append({
            "date": run_date,
            "ticker": row.get("symbol"),
            "stage_rejected": "correlation",
            "reason": row.get("clusterRejectReason") or "cluster_cap",
            "setup_score": row.get("setupQualityScore"),
            "near_miss": True,
            "price_scoring_eligible": True,
            "entry": row.get("price"),
            "stop": row.get("stopLoss"),
            "target": row.get("tp1"),
            "sector": row.get("sector"),
            "options_verdict": row.get("options_verdict"),
        })
    return out


def reject_rows_from_stage3(run_date: str, rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.append({
            "date": run_date,
            "ticker": row.get("symbol") or row.get("ticker"),
            "stage_rejected": "news_safety",
            "reason": row.get("stage3RejectReason") or "fresh_news_landmine",
            "detail": row.get("stage3RejectDetail"),
            "setup_score": row.get("setupQualityScore"),
            "near_miss": False,
            "price_scoring_eligible": False,
            "entry": row.get("price"),
            "stop": row.get("stopLoss"),
            "target": row.get("tp1"),
            "sector": row.get("sector"),
            "options_verdict": row.get("options_verdict"),
            "chronos_dir": row.get("chronos_dir"),
            "chronos_band_pct": row.get("chronos_band_pct"),
        })
    return out


def stage1_breakdown_from_counts(counts: dict, explicit: dict | None = None) -> dict:
    if explicit:
        return {
            "removed_volume": int(explicit.get("removed_volume", 0) or 0),
            "removed_price": int(explicit.get("removed_price", 0) or 0),
            "removed_dollar_vol": int(explicit.get("removed_dollar_vol", 0) or 0),
            "removed_earnings": int(explicit.get("removed_earnings", 0) or 0),
            "removed_other": int(explicit.get("removed_other", 0) or 0),
        }
    breakdown = {
        "removed_volume": 0,
        "removed_price": 0,
        "removed_dollar_vol": 0,
        "removed_earnings": 0,
        "removed_other": 0,
    }
    for reason, count in (counts or {}).items():
        n = int(count or 0)
        if reason == "avg_volume_below_1m":
            breakdown["removed_volume"] += n
        elif reason == "price_below_5":
            breakdown["removed_price"] += n
        elif reason == "dollar_volume_below_20m":
            breakdown["removed_dollar_vol"] += n
        elif str(reason).startswith("earnings_blackout"):
            breakdown["removed_earnings"] += n
        else:
            breakdown["removed_other"] += n
    return breakdown


def source_breakdown(universe: list, candidate_pool: list, catalyst_candidates: list) -> dict:
    counts = {
        "universe": len(universe),
        "catalyst": 0,
        "X": 0,
        "options_flag": 0,
        "vanta": 0,
    }
    for row in candidate_pool:
        if not isinstance(row, dict):
            continue
        src = row.get("source") or "scanner"
        if src == "catalyst":
            counts["catalyst"] += 1
        elif src in counts:
            counts[src] += 1
    if not counts["catalyst"] and catalyst_candidates:
        counts["catalyst"] = len(catalyst_candidates)
    return counts


def build_metadata(run_date: str) -> dict:
    regime = load_json("regime_check_latest.json", {})
    universe = load_json("universe.json", [])
    candidate_pool = load_json("candidate_pool.json", [])
    catalyst_candidates = load_json("catalyst_candidates.json", [])
    stage1 = load_json("stage1_survivors.json", [])
    stage1_meta = load_json("stage1_metadata.json", {})
    stage1_details = load_json("stage1_details.json", [])
    stage2_scored = load_json("stage2_surgical_strike_scored.json", [])
    stage2_top = load_json("stage2_surgical_strike_top40.json", [])
    stage2_meta = load_json("stage2_surgical_strike_metadata.json", {})
    stage3_top = load_json("stage3_news_safe_top40.json", [])
    stage3_rejections = load_json("stage3_news_rejections.json", [])
    stage3_meta = load_json("stage3_news_metadata.json", {})
    stage4_top = load_json("stage4_options_enriched_top40.json", [])
    stage4_meta = load_json("stage4_options_metadata.json", {})
    forecast_top = load_json("stage5_combined_forecast_top40.json", [])
    forecast_meta = load_json("stage5_combined_forecast_metadata.json", {})
    stage7 = load_json("stage7_clustered_survivors.json", [])
    stage7_rejections = load_json("stage7_cluster_rejections.json", [])
    stage7_report = load_json("stage7_cluster_report.json", {})

    universe_n = len(universe)
    stage1_n = len(stage1)
    stage2_n = len(stage2_top)
    stage3_n = len(stage3_top) if stage3_top else stage2_n
    stage4_n = len(stage4_top) or stage3_n
    stage5_n = len(forecast_top) or stage4_n
    stage7_n = len(stage7)
    stage6_n = stage5_n
    final_n = stage7_n

    stages = [
        {"stage": "Universe", "in": universe_n, "out": universe_n, "dropped": 0, "reason": "fetched candidates", "mode": "LIVE", "details": {"source_breakdown": source_breakdown(universe, candidate_pool, catalyst_candidates)}},
        {"stage": "Stage1", "in": universe_n, "out": stage1_n, "dropped": max(universe_n - stage1_n, 0), "reason": "cheap liquidity/price/earnings filter", "mode": "LIVE", "details": {"breakdown": stage1_breakdown_from_counts(stage1_meta.get("rejectCounts") or {}, stage1_meta.get("rejectionBreakdown"))}},
        {"stage": "Stage2", "in": stage1_n, "out": stage2_n, "dropped": max(stage1_n - stage2_n, 0), "reason": "weak setup / not top technical score"},
        {"stage": "Stage3 News", "in": stage2_n, "out": stage3_n, "dropped": max(stage2_n - stage3_n, 0), "reason": "news safety kill-filter before enrichment", "mode": "LIVE", "details": {"reason_counts": stage3_meta.get("reasonCounts") or {}, "no_data_count": stage3_meta.get("noDataCount"), "analyst_change_count": stage3_meta.get("analystChangeCount")}},
        {"stage": "Stage4 Options", "in": stage3_n, "out": stage4_n, "dropped": 0, "reason": "ride-along enrichment only", "mode": "RIDE-ALONG", "details": {"verdict_counts": stage4_meta.get("verdictCounts") or {}}},
        {"stage": "Stage5 Forecasts", "in": stage4_n, "out": stage5_n, "dropped": 0, "reason": "Chronos then Kronos; fail-open", "mode": "RIDE-ALONG", "details": forecast_meta},
        {"stage": "Stage6 Council", "in": stage5_n, "out": stage6_n, "dropped": 0, "reason": "not wired in baseline", "mode": "OFF"},
        {"stage": "Stage7 Correlation", "in": stage6_n, "out": stage7_n, "dropped": max(stage6_n - stage7_n, 0), "reason": "sector/ETF cluster concentration guard", "mode": "LIVE"},
        {"stage": "Finalists", "in": final_n, "out": final_n, "dropped": 0, "reason": "posted to scoring loop"},
    ]

    rejections = []
    rejections.extend(reject_rows_from_stage1(run_date, stage1_details))
    rejections.extend(reject_rows_from_stage2(run_date, stage2_scored, stage2_top))
    rejections.extend(reject_rows_from_stage7(run_date, stage7_rejections))
    rejections.extend(reject_rows_from_stage3(run_date, stage3_rejections))
    rejections = [r for r in rejections if r.get("ticker")]

    return {
        "date": run_date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "regime_checked_at": regime.get("checked_at"),
        "regime": regime.get("regime"),
        "regime_reason": regime.get("reason"),
        "regime_aborted": False,
        "spy_1d_pct": regime.get("spy_1d_pct"),
        "qqq_1d_pct": regime.get("qqq_1d_pct"),
        "vix_current": regime.get("vix_current"),
        "vix_1d_chg": regime.get("vix_1d_chg"),
        "position_size_multiplier": regime.get("position_size_multiplier"),
        "track_a_fail_count": stage2_meta.get("trackAFailCount", 0),
        "track_a_fail_symbols": stage2_meta.get("trackAFailSymbols", []),
        "stages": stages,
        "counts": {
            "universe": universe_n,
            "stage1": stage1_n,
            "stage2": stage2_n,
            "stage3": stage3_n,
            "stage4": stage4_n,
            "stage5": stage5_n,
            "stage6": stage6_n,
            "stage7": stage7_n,
            "finalists": final_n,
        },
        "stage1_reject_counts": stage1_meta.get("rejectCounts") or {},
        "stage1_breakdown": stage1_breakdown_from_counts(stage1_meta.get("rejectCounts") or {}, stage1_meta.get("rejectionBreakdown")),
        "stage3_news_safety": stage3_meta,
        "stage4_options_verdict_counts": stage4_meta.get("verdictCounts") or {},
        "chronos_inference_seconds": forecast_meta.get("chronos_inference_seconds"),
        "kronos_inference_seconds": forecast_meta.get("kronos_inference_seconds"),
        "total_forecast_seconds": forecast_meta.get("total_forecast_seconds"),
        "forecast_stage": forecast_meta,
        "source_breakdown": source_breakdown(universe, candidate_pool, catalyst_candidates),
        "stage7_report": stage7_report,
        "rejections": rejections,
        "near_miss_count": sum(1 for r in rejections if r.get("near_miss")),
        "safety_filter_active": True,
        "safety_filter_removed_count": len(stage3_rejections),
        "selection_logic_changed": False,
        "paper_only": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()
    metadata = build_metadata(args.date)
    response = post_json(args.url, metadata)
    print(json.dumps({
        "ok": response.get("ok"),
        "date": args.date,
        "stageCount": len(metadata["stages"]),
        "rejectionCount": len(metadata["rejections"]),
        "nearMissCount": metadata["near_miss_count"],
    }, indent=2))


if __name__ == "__main__":
    main()
