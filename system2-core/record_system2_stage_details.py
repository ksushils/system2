#!/usr/bin/env python3
"""
Record full stage-by-stage ticker transparency details.

Display/logging only. This reads artifacts already created by the nightly
pipeline and posts compact per-stage detail records to the fund-system API.
It performs no market-data calls and never changes trade selection.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "http://127.0.0.1:3210/api/system2/stage-detail"


def load_json(name: str, fallback):
    path = ROOT / name
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "system2-stage-detail-recorder/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def n(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def money(value) -> str:
    x = n(value)
    if x is None:
        return "-"
    if abs(x) >= 1_000_000:
        return f"{x/1_000_000:.2f}M"
    if abs(x) >= 1_000:
        return f"{x/1_000:.1f}K"
    return f"{x:.2f}"


def pct(value) -> str:
    x = n(value)
    return "-" if x is None else f"{x:+.2f}%"


def plain(value, digits=2) -> str:
    x = n(value)
    return "-" if x is None else f"{x:.{digits}f}"


def symbol(row: dict) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()


def stage1_reason(row: dict, config: dict) -> str:
    reasons = row.get("rejectReasons") or []
    parts = []
    min_price = config.get("minPrice", 5)
    min_volume = config.get("minAvgVolume", 1_000_000)
    min_dollar = config.get("minDollarVolume", 20_000_000)
    for reason in reasons:
        if reason == "avg_volume_below_1m":
            parts.append(f"avg_vol {money(row.get('averageVolume'))} below {money(min_volume)} floor")
        elif reason == "price_below_5":
            parts.append(f"price ${n(row.get('price'), 0):.2f} below ${n(min_price, 5):.2f} floor")
        elif reason == "dollar_volume_below_20m":
            parts.append(f"dollar_vol ${money(row.get('dollarVolume'))} below ${money(min_dollar)} floor")
        elif str(reason).startswith("earnings_blackout"):
            parts.append(f"earnings blackout inside next {config.get('earningsBlackoutDays', 5)}d")
        elif reason == "fund_or_etf":
            parts.append("fund/ETF excluded")
        elif reason == "inactive":
            parts.append("inactive security")
        elif reason == "blocked_non_common_equity":
            parts.append("blocked non-common equity")
        else:
            parts.append(str(reason))
    if parts:
        return "; ".join(parts)
    earnings = row.get("earningsDate") or "none in next window"
    return (
        f"price ${plain(row.get('price'))} OK >= ${plain(min_price)} | "
        f"avg_vol {money(row.get('averageVolume'))} OK >= {money(min_volume)} | "
        f"dollar_vol ${money(row.get('dollarVolume'))} OK >= ${money(min_dollar)} | "
        f"earnings {earnings}"
    )


def stage2_signal_reason(row: dict, score: float, cutoff: float | None) -> str:
    bits = [f"score {score:.0f}"]
    vwap = n(row.get("distanceFromVWAP"))
    rvol = n(row.get("volumeRatio"))
    rs = n(row.get("rsVsSpy"))
    sector = n(row.get("sectorAlpha"))
    atr = n(row.get("atrPct"))
    if vwap is None:
        bits.append("VWAP not captured")
    elif vwap < 0:
        bits.append(f"VWAP {pct(vwap)} below threshold")
    elif vwap > 4:
        bits.append(f"VWAP {pct(vwap)} extended")
    else:
        bits.append(f"VWAP {pct(vwap)} OK")
    if rs is None:
        bits.append("RS not captured")
    elif rs < 1:
        bits.append(f"RS {pct(rs)} weak")
    else:
        bits.append(f"RS {pct(rs)} OK")
    if rvol is None:
        bits.append("RVOL not captured")
    elif rvol < 1:
        bits.append(f"RVOL {rvol:.2f}x below 1x floor")
    else:
        bits.append(f"RVOL {rvol:.2f}x OK")
    if sector is not None and sector < 0:
        bits.append(f"sector RS {pct(sector)} weak")
    if atr is not None and atr > 6:
        bits.append(f"ATR {pct(atr)} high")
    if cutoff is not None:
        bits.append(f"top-40 cutoff {cutoff:.0f}")
    return " — ".join([bits[0], ", ".join(bits[1:])])


def options_threshold_note(row: dict) -> str:
    parts = []
    pc = n(row.get("put_call_vol_ratio"))
    if pc is not None:
        if pc < 0.70:
            read = "bullish"
        elif pc > 1.20:
            read = "bearish/caution"
        else:
            read = "neutral"
        parts.append(f"P/C {pc:.2f} — {read}, bullish threshold <0.70")
    skew = n(row.get("call_oi_skew"))
    if skew is not None:
        parts.append(f"call OI skew {skew:.2f}; extreme highlight threshold >50")
    ratio = n(row.get("call_vol_oi_ratio"))
    if ratio is not None:
        parts.append(f"call vol/OI {ratio:.2f}; unusual activity watch above 1.0")
    return "; ".join(parts)


def cone_flag(band) -> str:
    x = n(band)
    if x is None:
        return "NO_DATA"
    if x > 5:
        return "WIDE"
    if x >= 3:
        return "MODERATE"
    return "TIGHT"


def conviction_flag(value) -> str:
    x = n(value)
    if x is None:
        return "NO_DATA"
    if x >= 70:
        return "HIGH"
    if x >= 50:
        return "MEDIUM"
    return "LOW"


def build_universe() -> dict:
    universe = load_json("universe.json", [])
    candidate_pool = load_json("candidate_pool.json", [])
    rows = candidate_pool if candidate_pool else universe
    details = []
    for item in rows:
        row = item if isinstance(item, dict) else {"symbol": item, "source": "scanner"}
        details.append({
            "ticker": symbol(row),
            "status": "KEPT",
            "source": row.get("source") or "scanner",
            "catalyst_summary": row.get("catalyst_summary"),
            "entered_pool": True,
            "reason": "entered candidate pool",
            "data": row,
        })
    return stage_payload("Universe", "universe", details, {"sort": "ticker asc"})


def build_stage1() -> dict:
    rows = load_json("stage1_details.json", [])
    meta = load_json("stage1_metadata.json", {})
    config = meta.get("config") or {}
    details = []
    for row in rows:
        kept = row.get("status") == "PASS"
        details.append({
            "ticker": symbol(row),
            "source": row.get("source") or "scanner",
            "status": "KEPT" if kept else "REJECTED",
            "reason": stage1_reason(row, config),
            "price": row.get("price"),
            "avg_volume": row.get("averageVolume"),
            "dollar_volume": row.get("dollarVolume"),
            "earnings_date": row.get("earningsDate"),
            "earnings_in_days": "inside blackout" if row.get("earningsBlackoutNext5d") else "none in next window",
            "rejectReasons": row.get("rejectReasons") or [],
            "data": row,
        })
    details.sort(key=lambda r: (r["status"] == "KEPT", r["ticker"]))
    reject_counts = meta.get("rejectCounts") or {}
    breakdown = meta.get("rejectionBreakdown") or {
        "removed_volume": reject_counts.get("avg_volume_below_1m", 0),
        "removed_price": reject_counts.get("price_below_5", 0),
        "removed_dollar_vol": reject_counts.get("dollar_volume_below_20m", 0),
        "removed_earnings": sum(count for reason, count in reject_counts.items() if str(reason).startswith("earnings_blackout")),
        "removed_other": sum(
            count
            for reason, count in reject_counts.items()
            if reason not in {"avg_volume_below_1m", "price_below_5", "dollar_volume_below_20m"}
            and not str(reason).startswith("earnings_blackout")
        ),
    }
    return stage_payload("Stage1", "stage1", details, {"config": config, "rejectCounts": reject_counts, "rejectionBreakdown": breakdown})


def build_stage2() -> dict:
    scored = load_json("stage2_surgical_strike_scored.json", [])
    top = load_json("stage2_surgical_strike_top40.json", [])
    meta = load_json("stage2_surgical_strike_metadata.json", {})
    kept_symbols = {symbol(r) for r in top}
    top_scores = [n(r.get("setupQualityScore"), 0) for r in top]
    cutoff = min(top_scores) if top_scores else None
    details = []
    for row in scored:
        s = symbol(row)
        score = n(row.get("setupQualityScore"), 0)
        kept = s in kept_symbols
        if kept:
            reason = "in top-40 by setup score with RVOL/RS tiebreak"
        elif row.get("status") != "OK":
            reason = row.get("stage2RejectReason") or row.get("reason") or "technical scoring failed"
        else:
            reason = stage2_signal_reason(row, score, cutoff) if cutoff is not None else "not in top technical set"
        details.append({
            "ticker": s,
            "status": "KEPT" if kept else "REJECTED",
            "reason": reason,
            "setup_score": score,
            "grade": row.get("grade"),
            "setup_type": row.get("setupType") or row.get("setup"),
            "rvol": row.get("volumeRatio"),
            "rs_vs_spy": row.get("rsVsSpy"),
            "vwap_distance": row.get("distanceFromVWAP"),
            "atr": row.get("atr14"),
            "atr_pct": row.get("atrPct"),
            "sector": row.get("sector"),
            "sector_rs": row.get("sectorAlpha"),
            "data": row,
        })
    details.sort(key=lambda r: (-(r.get("setup_score") or 0), r["ticker"]))
    return stage_payload("Stage2", "stage2", details, {"scoreCutoff": cutoff, "scoreDistribution": meta.get("scoreDistribution") or {}, "fmpCalls": meta.get("fmpCalls") or {}})


def build_stage3() -> dict:
    rows = load_json("stage3_options_enriched_top40.json", [])
    meta = load_json("stage3_options_metadata.json", {})
    details = []
    for row in rows:
        status = "NO_DATA" if row.get("options_verdict") == "NO_DATA" else "ENRICHED"
        details.append({
            "ticker": symbol(row),
            "status": status,
            "reason": "ride-along enrichment only; no cut",
            "options_verdict": row.get("options_verdict"),
            "signals_count": row.get("options_signals_count"),
            "iv_rank": row.get("iv_rank_proxy") if row.get("iv_rank_proxy") is not None else row.get("iv_rank"),
            "call_vol_oi_ratio": row.get("call_vol_oi_ratio"),
            "put_call_vol_ratio": row.get("put_call_vol_ratio"),
            "call_oi_skew": row.get("call_oi_skew"),
            "options_notes": row.get("options_notes"),
            "threshold_notes": options_threshold_note(row),
            "data": row,
        })
    return stage_payload("Stage3 Options", "stage3", details, {"verdictCounts": meta.get("verdictCounts") or {}, "thresholds": {"bullish_put_call_vol_ratio": "<0.70", "extreme_call_oi_skew": ">50", "unusual_call_vol_oi_ratio": ">1.0"}})


def build_stage4() -> dict:
    rows = load_json("stage4_chronos_enriched_top40.json", [])
    meta = load_json("stage4_chronos_metadata.json", {})
    details = []
    for row in rows:
        has = all([row.get("chronos_dir"), row.get("chronos_band_pct") is not None, row.get("chronos2_1d"), row.get("chronos2_3d"), row.get("chronos2_5d")])
        band_flag = cone_flag(row.get("chronos_band_pct"))
        conf_flag = conviction_flag(row.get("forecastConviction"))
        details.append({
            "ticker": symbol(row),
            "status": "ENRICHED" if has else "NO_DATA",
            "reason": "ride-along enrichment only; no cut" if has else "Chronos unavailable or incomplete",
            "chronos_dir": row.get("chronos_dir"),
            "chronos_band_pct": row.get("chronos_band_pct"),
            "cone_flag": band_flag,
            "chronos2_1d": row.get("chronos2_1d"),
            "chronos2_3d": row.get("chronos2_3d"),
            "chronos2_5d": row.get("chronos2_5d"),
            "forecastConviction": row.get("forecastConviction"),
            "conviction_flag": conf_flag,
            "data": row,
        })
    dirs = {"UP": 0, "DOWN": 0, "FLAT": 0}
    bands = []
    confs = []
    cone_counts = {"WIDE": 0, "MODERATE": 0, "TIGHT": 0, "NO_DATA": 0}
    for row in details:
        if row.get("chronos_dir") in dirs:
            dirs[row["chronos_dir"]] += 1
        if n(row.get("chronos_band_pct")) is not None:
            bands.append(n(row.get("chronos_band_pct")))
        if n(row.get("forecastConviction")) is not None:
            confs.append(n(row.get("forecastConviction")))
        cone_counts[row.get("cone_flag") or "NO_DATA"] += 1
    return stage_payload("Stage4 Chronos", "stage4", details, {"modelVersions": meta.get("modelVersions") or {}, "fmpOhlcvCallCount": meta.get("fmpOhlcvCallCount"), "directionCounts": dirs, "avgBandPct": round(sum(bands) / len(bands), 3) if bands else None, "avgConviction": round(sum(confs) / len(confs), 2) if confs else None, "coneCounts": cone_counts, "convictionThresholds": {"high": ">=70", "medium": "50-69", "low": "<50"}})


def build_stage5() -> dict:
    safe = load_json("stage5_news_safe_finalists.json", [])
    rejected = load_json("stage5_news_rejections.json", [])
    meta = load_json("stage5_news_metadata.json", {})
    details = []
    for row in safe:
        checked = row.get("news_safety_status") not in (None, "NO_DATA")
        details.append({
            "ticker": symbol(row),
            "source": row.get("source") or "scanner",
            "status": "KEPT",
            "reason": "no fresh hard landmine found" if checked else "NO_DATA fail-safe kept ticker",
            "news_checked": checked,
            "analyst_change": row.get("analyst_change"),
            "hard_landmine": row.get("hard_landmine"),
            "news_recent_items_checked": row.get("news_recent_items_checked"),
            "news_items": row.get("news_items") or [],
            "fetch_failed": row.get("news_safety_status") == "NO_DATA",
            "data": row,
        })
    for row in rejected:
        detail = row.get("stage5RejectDetail") or {}
        details.append({
            "ticker": symbol(row),
            "source": row.get("source") or "scanner",
            "status": "REJECTED",
            "reason": f"{row.get('stage5RejectReason')}: {detail.get('summary') or 'fresh hard landmine'}",
            "news_checked": True,
            "analyst_change": row.get("analyst_change"),
            "hard_landmine": detail,
            "news_recent_items_checked": row.get("news_recent_items_checked"),
            "news_items": row.get("news_items") or [],
            "fetch_failed": False,
            "data": row,
        })
    details.sort(key=lambda r: (r["status"] == "KEPT", r["ticker"]))
    return stage_payload("Stage5 News", "stage5", details, {"reasonCounts": meta.get("reasonCounts") or {}, "noDataCount": meta.get("noDataCount"), "fmpCallCount": meta.get("fmpCallCount"), "analystChangeCount": meta.get("analystChangeCount")})


def build_stage6() -> dict:
    rows = load_json("stage5_news_safe_finalists.json", []) or load_json("stage7_clustered_survivors.json", [])
    details = []
    for row in rows:
        details.append({
            "ticker": symbol(row),
            "status": "OFF",
            "reason": "Council not wired in baseline",
            "council_votes": row.get("council_votes"),
            "council_conf": row.get("council_conf"),
            "claude_verdict": row.get("claude_verdict"),
            "gpt_verdict": row.get("gpt_verdict"),
            "gemini_verdict": row.get("gemini_verdict"),
            "red_flags": row.get("red_flags") or [],
            "council_reasoning_summary": row.get("council_reasoning_summary"),
            "data": row,
        })
    return stage_payload("Stage6 Council", "stage6", details, {"mode": "OFF"})


def build_stage7() -> dict:
    kept = load_json("stage7_clustered_survivors.json", [])
    rejected = load_json("stage7_cluster_rejections.json", [])
    report = load_json("stage7_cluster_report.json", {})
    kept_symbols = {symbol(row) for row in kept}
    details = []
    for row in kept:
        cluster = row.get("cluster") or {}
        details.append({
            "ticker": symbol(row),
            "source": row.get("source") or "scanner",
            "status": "KEPT",
            "reason": f"kept rank {cluster.get('clusterRank')} in {cluster.get('sector')} cluster",
            "cluster": cluster,
            "setup_score": row.get("setupQualityScore"),
            "volumeRatio": row.get("volumeRatio"),
            "rsVsSpy": row.get("rsVsSpy"),
            "data": row,
        })
    for row in rejected:
        cluster = row.get("cluster") or {}
        sector = cluster.get("sector") or row.get("sector")
        same_cluster_kept = [r for r in kept if (r.get("cluster") or {}).get("sector") == sector]
        kept_text = ", ".join(f"{symbol(r)}({r.get('setupQualityScore')})" for r in same_cluster_kept)
        details.append({
            "ticker": symbol(row),
            "source": row.get("source") or "scanner",
            "status": "REJECTED",
            "reason": f"{sector} cluster kept {kept_text}; removed {symbol(row)}({row.get('setupQualityScore')})",
            "tiebreak": "same score ties are ordered by setup score, then RVOL, then RS vs SPY",
            "cluster": cluster,
            "setup_score": row.get("setupQualityScore"),
            "volumeRatio": row.get("volumeRatio"),
            "rsVsSpy": row.get("rsVsSpy"),
            "data": row,
        })
    details.sort(key=lambda r: ((r.get("cluster") or {}).get("sector") or "", r["status"] == "REJECTED", -(r.get("setup_score") or 0), r["ticker"]))
    clusters = report.get("clusters") or []
    largest = sorted(clusters, key=lambda c: c.get("rawCount", 0), reverse=True)[0] if clusters else None
    return stage_payload("Stage7 Correlation", "stage7", details, {"clusters": clusters, "clusterCount": len(clusters), "largestCluster": largest, "tiebreak": ["setupQualityScore desc", "volumeRatio desc", "rsVsSpy desc"], "riskRulesUnchanged": report.get("riskRulesUnchanged") or {}})


def build_finalists() -> dict:
    rows = load_json("stage5_news_safe_finalists.json", []) or load_json("stage7_clustered_survivors.json", [])
    details = [{
        "ticker": symbol(row),
        "status": "FINALIST",
        "reason": "final paper-mode idea after safety/risk funnel",
        "data": row,
    } for row in rows]
    return stage_payload("Finalists", "finalists", details, {"tradeCards": True})


def stage_payload(name: str, key: str, details: list[dict], metadata: dict | None = None) -> dict:
    entered = len(details)
    kept = sum(1 for row in details if row.get("status") in {"KEPT", "ENRICHED", "FINALIST", "OFF"})
    rejected = sum(1 for row in details if row.get("status") == "REJECTED")
    no_data = sum(1 for row in details if row.get("status") == "NO_DATA")
    # The full raw ticker row is never needed for stage transparency display.
    # Removing it prevents fund.json from growing by several MB per pipeline run.
    for row in details:
        row.pop("data", None)
    return {
        "stage": name,
        "stage_key": key,
        "entered": entered,
        "kept": kept,
        "rejected": rejected,
        "no_data": no_data,
        "metadata": metadata or {},
        "tickers": details,
    }


def build_all(date: str) -> dict:
    stages = [
        build_universe(),
        build_stage1(),
        build_stage2(),
        build_stage4(),
        build_stage3(),
        build_stage7(),
        build_stage5(),
        build_stage6(),
        build_finalists(),
    ]
    return {
        "date": date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "selection_logic_changed": False,
        "stages": stages,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--output", default=str(ROOT / "stage_detail_latest.json"))
    args = parser.parse_args()
    payload = build_all(args.date)
    Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        response = post_json(args.url, payload)
        posted = bool(response.get("ok"))
        error = None
    except Exception as exc:
        posted = False
        error = str(exc)
    print(json.dumps({
        "date": args.date,
        "posted": posted,
        "error": error,
        "stageCount": len(payload["stages"]),
        "stage2Rows": next(s for s in payload["stages"] if s["stage_key"] == "stage2")["entered"],
    }, indent=2))


if __name__ == "__main__":
    main()
