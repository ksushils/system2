#!/usr/bin/env python3
"""
System 2 — Performance Analytics Engine (Prompt 3 of 5).

Computes professional-grade performance metrics from fund.json:
- Scanner quality (Idea-R)
- Trading performance (Trade-R, slippage-adjusted)
- MFE/MAE analysis
- R distribution
- Council calibration
- Gate audit
- Portfolio exposure tracker

Usage:
  python3 performance_analytics.py [--legacy]

Output:
  /root/system2-core/data/performance_metrics.json
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FUND_PATH = Path("/root/fund-system/data/fund.json")
OUTPUT_PATH = Path("/root/system2-core/data/performance_metrics.json")


def load_fund() -> list[dict]:
    data = json.loads(FUND_PATH.read_text(encoding="utf-8"))
    return data.get("ideas", []) if isinstance(data, dict) else data


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        v = float(value)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def safe_div(a: float, b: float) -> float | None:
    return a / b if b and b != 0 else None


def measurement_population(row: dict) -> str:
    return "ENTERED_TRADE" if row.get("trade_entered") is True else "WATCHLIST_UNENTERED"


def avg_field(rows: list[dict], field: str) -> float | None:
    vals = [num(r.get(field)) for r in rows if r.get(field) is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def count_field(rows: list[dict], field: str) -> int:
    return sum(1 for r in rows if r.get(field) is not None)


def compute_measurement_populations(ideas: list[dict]) -> dict[str, Any]:
    entered = [i for i in ideas if measurement_population(i) == "ENTERED_TRADE"]
    watchlist = [i for i in ideas if measurement_population(i) == "WATCHLIST_UNENTERED"]
    return {
        "entered_trades": {
            "count": len(entered),
            "legacy_avg_canonical_r": avg_field(entered, "canonical_r"),
            "legacy_avg_r_3d": avg_field(entered, "r_3d"),
            "avg_trade_r_net": avg_field(entered, "trade_r_net"),
        },
        "watchlist_unentered": {
            "count": len(watchlist),
            "legacy_avg_canonical_r": avg_field(watchlist, "canonical_r"),
            "legacy_avg_r_3d": avg_field(watchlist, "r_3d"),
            "would_be_r_markout_3d_count": count_field(watchlist, "would_be_r_markout_3d"),
            "avg_would_be_r_markout_3d": avg_field(watchlist, "would_be_r_markout_3d"),
            "would_be_r_markout_5d_count": count_field(watchlist, "would_be_r_markout_5d"),
            "avg_would_be_r_markout_5d": avg_field(watchlist, "would_be_r_markout_5d"),
            "would_be_r_markout_10d_count": count_field(watchlist, "would_be_r_markout_10d"),
            "avg_would_be_r_markout_10d": avg_field(watchlist, "would_be_r_markout_10d"),
        },
        "note": "Entered trade R and unentered directional mark-outs are reported separately and are not averaged together.",
    }


def compute_scanner_quality(ideas: list[dict]) -> dict[str, Any]:
    total = len(ideas)
    resolved = [i for i in ideas if i.get("idea_r") is not None]
    resolved_count = len(resolved)

    hit_tp1 = sum(1 for i in resolved if i.get("idea_outcome") == "winner")
    hit_stop = sum(1 for i in resolved if i.get("idea_outcome") == "loser")
    unentered = sum(1 for i in ideas if not i.get("trade_entered", False))

    def avg_r(rows: list[dict]) -> float | None:
        vals = [num(r.get("idea_r")) for r in rows if r.get("idea_r") is not None]
        return sum(vals) / len(vals) if vals else None

    return {
        "scanner_ideas_total": total,
        "scanner_ideas_resolved": resolved_count,
        "scanner_hit_tp1_rate": round(safe_div(hit_tp1, resolved_count) * 100, 1) if resolved_count else None,
        "scanner_hit_stop_rate": round(safe_div(hit_stop, resolved_count) * 100, 1) if resolved_count else None,
        "scanner_unentered_rate": round(safe_div(unentered, total) * 100, 1) if total else None,
        "scanner_avg_idea_r": round(avg_r(resolved), 3) if resolved else None,
        "scanner_avg_r_set1": round(avg_r([i for i in resolved if i.get("set") == 1]), 3) if any(i.get("set") == 1 for i in resolved) else None,
        "scanner_avg_r_set2": round(avg_r([i for i in resolved if i.get("set") == 2]), 3) if any(i.get("set") == 2 for i in resolved) else None,
        "scanner_avg_r_multiset": round(avg_r([i for i in resolved if i.get("multi_set_idea")]), 3) if any(i.get("multi_set_idea") for i in resolved) else None,
    }


def compute_trading_performance(ideas: list[dict]) -> dict[str, Any]:
    trades = [i for i in ideas if i.get("trade_entered") is True]
    total_trades = len(trades)
    if not total_trades:
        return {"trades_taken": 0, "note": "No trades taken yet"}

    won = [t for t in trades if num(t.get("trade_r_net"), -1) > 0]
    lost = [t for t in trades if num(t.get("trade_r_net"), 0) <= 0]

    win_rate = safe_div(len(won), total_trades)

    r_nets = [num(t.get("trade_r_net")) for t in trades]
    r_grosses = [num(t.get("trade_r_gross")) for t in trades]
    r_wins = [num(t.get("trade_r_net")) for t in won]
    r_losses = [abs(num(t.get("trade_r_net"))) for t in lost]

    avg_win_r = sum(r_wins) / len(r_wins) if r_wins else 0
    avg_loss_r = sum(r_losses) / len(r_losses) if r_losses else 0

    expectancy = (win_rate * avg_win_r) - ((1 - win_rate) * avg_loss_r) if win_rate is not None else None

    positive_r = sum(r for r in r_nets if r > 0)
    negative_r = sum(abs(r) for r in r_nets if r < 0)
    profit_factor = safe_div(positive_r, negative_r)

    # Drawdown from cumulative equity curve
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    current_dd = 0.0
    for t in sorted(trades, key=lambda x: x.get("paper_exit_at") or x.get("scored_at") or ""):
        cumulative += num(t.get("trade_r_net"))
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
        current_dd = dd

    # Streaks
    streaks = []
    current_streak = 0
    for t in sorted(trades, key=lambda x: x.get("paper_exit_at") or x.get("scored_at") or ""):
        r = num(t.get("trade_r_net"))
        if r > 0:
            if current_streak >= 0:
                current_streak += 1
            else:
                streaks.append(current_streak)
                current_streak = 1
        else:
            if current_streak <= 0:
                current_streak -= 1
            else:
                streaks.append(current_streak)
                current_streak = -1
    streaks.append(current_streak)
    wins_streaks = [s for s in streaks if s > 0]
    loss_streaks = [abs(s) for s in streaks if s < 0]

    slippage_vals = [num(t.get("slippage_r")) for t in trades]
    total_slippage = sum(slippage_vals)

    return {
        "trades_taken": total_trades,
        "trades_won": len(won),
        "win_rate": round(win_rate * 100, 1) if win_rate is not None else None,
        "avg_r_gross": round(sum(r_grosses) / len(r_grosses), 3) if r_grosses else None,
        "avg_r_net": round(sum(r_nets) / len(r_nets), 3) if r_nets else None,
        "median_r_net": round(sorted(r_nets)[len(r_nets) // 2], 3) if r_nets else None,
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "expectancy": round(expectancy, 3) if expectancy is not None else None,
        "total_slippage_r": round(total_slippage, 3),
        "slippage_impact": round(total_slippage / total_trades, 3) if total_trades else None,
        "max_drawdown_r": round(max_dd, 3),
        "current_drawdown_r": round(current_dd, 3),
        "consecutive_wins_max": max(wins_streaks) if wins_streaks else 0,
        "consecutive_losses_max": max(loss_streaks) if loss_streaks else 0,
        "current_streak": current_streak,
    }


def compute_mfe_mae(trades: list[dict]) -> dict[str, Any]:
    if len(trades) < 15:
        return {"note": f"Need {15 - len(trades)} more trades for MFE/MAE analysis"}

    mfe_rs = [num(t.get("mfe_r")) for t in trades if t.get("mfe_r") is not None]
    mae_rs = [num(t.get("mae_r")) for t in trades if t.get("mae_r") is not None]
    captures = [num(t.get("capture_rate")) for t in trades if t.get("capture_rate") is not None]

    # Stop quality: % of losers where mfe_r > 0.5 before stop hit
    losers = [t for t in trades if num(t.get("trade_r_net"), 0) <= 0]
    stop_quality = safe_div(
        sum(1 for t in losers if num(t.get("mfe_r"), 0) > 0.5),
        len(losers)
    )

    # Exit quality: % of winners where capture_rate > 0.5
    winners = [t for t in trades if num(t.get("trade_r_net"), 0) > 0]
    exit_quality = safe_div(
        sum(1 for t in winners if num(t.get("capture_rate"), 0) > 0.5),
        len(winners)
    )

    def bucket_mfe(v: float) -> str:
        if v <= 0.5:
            return "0-0.5R"
        if v <= 1.0:
            return "0.5-1R"
        if v <= 2.0:
            return "1-2R"
        if v <= 3.0:
            return "2-3R"
        return "3R+"

    def bucket_mae(v: float) -> str:
        if v <= 0.25:
            return "0-0.25R"
        if v <= 0.5:
            return "0.25-0.5R"
        if v <= 1.0:
            return "0.5-1R"
        return "1R+"

    return {
        "avg_mfe_r": round(sum(mfe_rs) / len(mfe_rs), 3) if mfe_rs else None,
        "avg_mae_r": round(sum(mae_rs) / len(mae_rs), 3) if mae_rs else None,
        "avg_capture_rate": round(sum(captures) / len(captures) * 100, 1) if captures else None,
        "stop_quality_score": round(stop_quality * 100, 1) if stop_quality is not None else None,
        "exit_quality_score": round(exit_quality * 100, 1) if exit_quality is not None else None,
        "mfe_buckets": dict(Counter(bucket_mfe(v) for v in mfe_rs)),
        "mae_buckets": dict(Counter(bucket_mae(v) for v in mae_rs)),
    }


def compute_r_distribution(trades: list[dict]) -> dict[str, int]:
    nets = [num(t.get("trade_r_net")) for t in trades]
    return {
        "below_negative_2": sum(1 for r in nets if r < -2),
        "negative_2_to_1": sum(1 for r in nets if -2 <= r < -1),
        "negative_1_to_0": sum(1 for r in nets if -1 <= r < 0),
        "zero_to_1": sum(1 for r in nets if 0 <= r < 1),
        "1_to_2": sum(1 for r in nets if 1 <= r < 2),
        "2_to_3": sum(1 for r in nets if 2 <= r < 3),
        "above_3": sum(1 for r in nets if r >= 3),
    }


def compute_council_calibration(ideas: list[dict]) -> dict[str, Any]:
    models = ["gemini", "claude", "kimi", "gpt4o"]
    result = {}

    for mk in models:
        verdicts = Counter()
        by_verdict: dict[str, list[dict]] = {}
        for i in ideas:
            v = str(i.get(f"{mk}_verdict") or i.get(f"{mk}_r2_verdict") or "").strip().upper()
            if not v:
                v = "ABSTAIN"
            verdicts[v] += 1
            by_verdict.setdefault(v, []).append(i)

        total_verdicts = sum(verdicts.values())
        if total_verdicts < 10:
            result[mk] = {"note": f"Only {total_verdicts} verdicts — need 10+"}
            continue

        stats = {}
        for v, rows in by_verdict.items():
            nets = [num(r.get("trade_r_net")) for r in rows if r.get("trade_r_net") is not None]
            wins = sum(1 for n in nets if n > 0)
            stats[v] = {
                "count": len(rows),
                "avg_r_net": round(sum(nets) / len(nets), 3) if nets else None,
                "win_rate": round(wins / len(nets) * 100, 1) if nets else None,
            }

        tier1_avg = stats.get("TIER1", {}).get("avg_r_net")
        skip_avg = stats.get("SKIP", {}).get("avg_r_net") or stats.get("FORCE_SKIP", {}).get("avg_r_net")
        calibrated = (tier1_avg is not None and tier1_avg > 0) and (skip_avg is not None and skip_avg < 0)

        result[mk] = {
            "verdicts": dict(verdicts),
            "by_verdict": stats,
            "calibrated": calibrated,
            "total_verdicts": total_verdicts,
        }

    # Overall council value
    non_empty = [v for v in result.values() if isinstance(v, dict) and "by_verdict" in v]
    council_adds_value = False
    best_model = None
    best_delta = -999
    if non_empty:
        for mk, data in result.items():
            if not isinstance(data, dict) or "by_verdict" not in data:
                continue
            tier1 = data["by_verdict"].get("TIER1", {}).get("avg_r_net")
            non_tier1_vals = [v.get("avg_r_net") for k, v in data["by_verdict"].items() if k != "TIER1" and v.get("avg_r_net") is not None]
            non_tier1_avg = sum(non_tier1_vals) / len(non_tier1_vals) if non_tier1_vals else None
            if tier1 is not None and non_tier1_avg is not None:
                delta = tier1 - non_tier1_avg
                if delta > best_delta:
                    best_delta = delta
                    best_model = mk
                if delta > 0.3:
                    council_adds_value = True

    return {
        "per_model": result,
        "council_adds_value": council_adds_value,
        "best_model": best_model,
        "best_model_delta_r": round(best_delta, 3) if best_model else None,
    }


def compute_gate_audit(ideas: list[dict]) -> dict[str, Any]:
    rejected = [i for i in ideas if i.get("stage2RejectReason")]
    if not rejected:
        return {"note": "No rejected ideas with stage2RejectReason in data"}

    gates: dict[str, list[dict]] = {}
    for i in rejected:
        reason = i.get("stage2RejectReason", "unknown")
        gates.setdefault(reason, []).append(i)

    audit = {}
    for reason, rows in gates.items():
        returns = [num(r.get("r_5d")) for r in rows if r.get("r_5d") is not None]
        if not returns:
            returns = [num(r.get("r_3d")) for r in rows if r.get("r_3d") is not None]
        avg_ret = sum(returns) / len(returns) if returns else None
        audit[reason] = {
            "count": len(rows),
            "shadow_avg_5d_return_pct": round(avg_ret, 2) if avg_ret is not None else None,
            "gate_useful": avg_ret is not None and avg_ret < 0,
            "gate_review_flag": avg_ret is not None and avg_ret > 1.5,
        }
    return audit


def compute_portfolio_exposure(ideas: list[dict]) -> dict[str, Any]:
    """Compute current portfolio exposure from open entered trades."""
    open_trades = [i for i in ideas if i.get("trade_entered") is True and i.get("paper_status") == "OPEN"]
    total_entered = len(open_trades)

    if not total_entered:
        return {
            "total_entered_trades": 0,
            "total_open_r": 0,
            "by_sector": {},
            "by_set": {},
            "max_sector_concentration": 0,
            "concentration_warning": False,
            "risk_warning": False,
        }

    total_open_r = sum(num(t.get("position_r", 1.0)) for t in open_trades)

    by_sector: dict[str, dict] = defaultdict(lambda: {"count": 0, "r": 0.0})
    by_set: dict[str, dict] = {"set1": {"count": 0, "r": 0.0}, "set2": {"count": 0, "r": 0.0}}

    for t in open_trades:
        sector = t.get("sector") or t.get("cluster_sector") or "Unknown"
        by_sector[sector]["count"] += 1
        by_sector[sector]["r"] += num(t.get("position_r", 1.0))

        s = t.get("set", 1)
        if t.get("multi_set_idea"):
            by_set["set1"]["count"] += 1
            by_set["set1"]["r"] += num(t.get("position_r", 1.0))
            by_set["set2"]["count"] += 1
            by_set["set2"]["r"] += num(t.get("position_r", 1.0))
        elif s == 2:
            by_set["set2"]["count"] += 1
            by_set["set2"]["r"] += num(t.get("position_r", 1.0))
        else:
            by_set["set1"]["count"] += 1
            by_set["set1"]["r"] += num(t.get("position_r", 1.0))

    max_sector_count = max((v["count"] for v in by_sector.values()), default=0)
    concentration_warning = max_sector_count >= 3
    risk_warning = total_open_r > 10

    # Format for output
    by_sector_out = {k: {"count": v["count"], "r": round(v["r"], 2)} for k, v in by_sector.items()}
    by_set_out = {k: {"count": v["count"], "r": round(v["r"], 2)} for k, v in by_set.items()}

    return {
        "total_entered_trades": total_entered,
        "total_open_r": round(total_open_r, 2),
        "by_sector": by_sector_out,
        "by_set": by_set_out,
        "max_sector_concentration": max_sector_count,
        "concentration_warning": concentration_warning,
        "risk_warning": risk_warning,
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", action="store_true", help="Include legacy-era ideas")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    args = parser.parse_args()

    all_ideas = load_fund()
    era_filter = None if args.legacy else "system2_v2"
    ideas = [i for i in all_ideas if not era_filter or i.get("era") == era_filter]

    scanner = compute_scanner_quality(ideas)
    trading = compute_trading_performance(ideas)
    trades = [i for i in ideas if i.get("trade_entered") is True]
    mfe_mae = compute_mfe_mae(trades)
    r_dist = compute_r_distribution(trades)
    council = compute_council_calibration(ideas)
    gate_audit = compute_gate_audit(all_ideas)
    portfolio = compute_portfolio_exposure(ideas)
    measurement_populations = compute_measurement_populations(ideas)

    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "era_filter": era_filter or "all",
        "idea_count": len(ideas),
        "scanner": scanner,
        "trading": trading,
        "mfe_mae": mfe_mae,
        "r_distribution": r_dist,
        "council": council,
        "gate_audit": gate_audit,
        "portfolio_exposure": portfolio,
        "measurement_populations": measurement_populations,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": True, "ideas": len(ideas), "trades": len(trades), "path": str(OUTPUT_PATH)}, indent=2))


if __name__ == "__main__":
    main()
