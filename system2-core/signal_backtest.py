#!/usr/bin/env python3
"""
System 2 — Historical Signal Validation Backtest.

Reads resolved ideas from fund.json and computes win rate / avg R / profit factor
across 8 signal slices to identify which signals actually predicted winners.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

FUND_PATH = Path("/root/fund-system/data/fund.json")
REPORT_DIR = Path("/root/system2-core/reports")
DATA_DIR = Path("/root/system2-core/data")
MIN_SAMPLE = 5

# ═══════════════════════════════════════════════════════════════════════════════
# LOAD DATA
# ═══════════════════════════════════════════════════════════════════════════════


def load_fund() -> list[dict[str, Any]]:
    if not FUND_PATH.exists():
        return []
    data = json.loads(FUND_PATH.read_text(encoding="utf-8"))
    return data.get("ideas", data.get("data", {}).get("ideas", []))


def get_outcome_r(idea: dict[str, Any]) -> float | None:
    """Return best available R multiple for this idea."""
    for key in ["actual_r", "r_3d", "r_1d", "r_10d"]:
        v = idea.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def is_winner(idea: dict[str, Any]) -> bool | None:
    """Determine if idea was a winner."""
    r = get_outcome_r(idea)
    if r is not None:
        return r > 0
    outcome = str(idea.get("paper_outcome") or "").upper()
    if outcome == "WIN":
        return True
    if outcome == "LOSS":
        return False
    hit = str(idea.get("hit") or "").upper()
    if hit == "TARGET":
        return True
    if hit in {"STOP", "TIME", "TIMEOUT"}:
        return False
    return None


def get_rvol(idea: dict[str, Any]) -> float | None:
    """Extract RVOL from various possible field names."""
    for key in ["volumeRatio", "rvol", "rvol_20d", "relative_volume"]:
        v = idea.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    # Try nested momentum structure
    momentum = idea.get("momentum") or {}
    if isinstance(momentum, dict):
        for key in ["volumeRatio", "rvol", "relative_volume"]:
            v = momentum.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
    return None


def get_setup_score(idea: dict[str, Any]) -> float | None:
    """Extract setup score from various fields."""
    for key in ["setup_score", "setupQualityScore", "convictionScore", "core_setup_score"]:
        v = idea.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def get_confluence_score(idea: dict[str, Any]) -> float | None:
    """Extract confluence/trade quality score."""
    for key in ["confluence_score", "trade_quality_score"]:
        v = idea.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def get_chronos_dir(idea: dict[str, Any]) -> str:
    """Extract chronos/forecast direction."""
    for key in ["combined_forecast_dir", "chronos_dir", "forecastDecision"]:
        v = idea.get(key)
        if v:
            return str(v).strip().upper()
    return "UNKNOWN"


def get_options_verdict(idea: dict[str, Any]) -> str:
    """Extract options verdict."""
    v2 = idea.get("options_verdict_v2")
    if v2:
        return str(v2).strip().upper()
    v = idea.get("options_verdict")
    if v:
        return str(v).strip().upper()
    return "UNKNOWN"


def get_days_held(idea: dict[str, Any]) -> int | None:
    """Compute approximate days held from available dates."""
    date_str = idea.get("date")
    exit_str = idea.get("paper_exit_at") or idea.get("scored_at")
    if not date_str or not exit_str:
        return None
    try:
        d1 = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(str(exit_str).replace("Z", "+00:00"))
        return max(0, (d2.date() - d1.date()).days)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SLICE COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════════


def compute_slice_stats(ideas: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute statistics for a bucket of ideas."""
    rs = [get_outcome_r(i) for i in ideas]
    rs_valid = [r for r in rs if r is not None]
    winners = [i for i in ideas if is_winner(i) is True]
    losers = [i for i in ideas if is_winner(i) is False]

    count = len(ideas)
    if count < MIN_SAMPLE:
        return {"count": count, "note": f"insufficient sample ({count} ideas)"}

    win_rate = round(len(winners) / count * 100, 1) if count > 0 else 0
    avg_r = round(sum(rs_valid) / len(rs_valid), 2) if rs_valid else None
    med_r = round(median(rs_valid), 2) if len(rs_valid) >= 1 else None

    pos_r = sum(r for r in rs_valid if r > 0)
    neg_r = abs(sum(r for r in rs_valid if r < 0))
    pf = round(pos_r / neg_r, 2) if neg_r > 0 else (99.99 if pos_r > 0 else 0)

    best_r = round(max(rs_valid), 2) if rs_valid else None
    worst_r = round(min(rs_valid), 2) if rs_valid else None

    return {
        "count": count,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "median_r": med_r,
        "profit_factor": pf,
        "best_r": best_r,
        "worst_r": worst_r,
    }


def slice_setup_score(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = {
        "setup_60_to_75": [],
        "setup_75_to_85": [],
        "setup_85_plus": [],
        "setup_under_60": [],
    }
    for i in ideas:
        s = get_setup_score(i)
        if s is None:
            continue
        if s >= 85:
            buckets["setup_85_plus"].append(i)
        elif s >= 75:
            buckets["setup_75_to_85"].append(i)
        elif s >= 60:
            buckets["setup_60_to_75"].append(i)
        else:
            buckets["setup_under_60"].append(i)

    results = []
    for name, bucket in buckets.items():
        stats = compute_slice_stats(bucket)
        stats["name"] = name
        results.append(stats)
    return results


def slice_confluence_score(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = {
        "confluence_40_55": [],
        "confluence_55_70": [],
        "confluence_70_plus": [],
        "confluence_under_40": [],
    }
    for i in ideas:
        c = get_confluence_score(i)
        if c is None:
            continue
        if c >= 70:
            buckets["confluence_70_plus"].append(i)
        elif c >= 55:
            buckets["confluence_55_70"].append(i)
        elif c >= 40:
            buckets["confluence_40_55"].append(i)
        else:
            buckets["confluence_under_40"].append(i)

    results = []
    for name, bucket in buckets.items():
        stats = compute_slice_stats(bucket)
        stats["name"] = name
        if name == "confluence_40_55":
            stats["min"] = 40
        elif name == "confluence_55_70":
            stats["min"] = 55
        elif name == "confluence_70_plus":
            stats["min"] = 70
        results.append(stats)
    return results


def slice_options_verdict(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = defaultdict(list)
    for i in ideas:
        v = get_options_verdict(i)
        buckets[v].append(i)

    # Normalize names
    name_map = {
        "CONFIRM": "options_confirm",
        "STRONG_BULLISH_CONFIRM": "options_strong_confirm",
        "BULLISH_CONFIRM": "options_bullish_confirm",
        "NEUTRAL": "options_neutral",
        "CAUTION": "options_caution",
        "BEARISH_WARNING": "options_bearish",
        "NO_DATA": "options_no_data",
        "UNKNOWN": "options_unknown",
    }

    results = []
    for raw_name, bucket in buckets.items():
        stats = compute_slice_stats(bucket)
        stats["name"] = name_map.get(raw_name, f"options_{raw_name.lower()}")
        results.append(stats)
    return results


def slice_rvol(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = {
        "rvol_under_2": [],
        "rvol_2_to_4": [],
        "rvol_4_to_7": [],
        "rvol_7_plus": [],
        "rvol_unknown": [],
    }
    for i in ideas:
        r = get_rvol(i)
        if r is None:
            buckets["rvol_unknown"].append(i)
        elif r >= 7:
            buckets["rvol_7_plus"].append(i)
        elif r >= 4:
            buckets["rvol_4_to_7"].append(i)
        elif r >= 2:
            buckets["rvol_2_to_4"].append(i)
        else:
            buckets["rvol_under_2"].append(i)

    results = []
    for name, bucket in buckets.items():
        stats = compute_slice_stats(bucket)
        stats["name"] = name
        results.append(stats)
    return results


def slice_chronos_dir(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = defaultdict(list)
    for i in ideas:
        d = get_chronos_dir(i)
        buckets[d].append(i)

    name_map = {
        "STRONG_UP": "chronos_strong_up",
        "UP": "chronos_up",
        "LEAN_UP": "chronos_lean_up",
        "FLAT": "chronos_flat",
        "LEAN_DOWN": "chronos_lean_down",
        "DOWN": "chronos_down",
        "STRONG_DOWN": "chronos_strong_down",
        "REJECT": "chronos_reject",
        "UNKNOWN": "chronos_unknown",
    }

    results = []
    for raw_name, bucket in buckets.items():
        stats = compute_slice_stats(bucket)
        stats["name"] = name_map.get(raw_name, f"chronos_{raw_name.lower()}")
        results.append(stats)
    return results


def slice_sector(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = defaultdict(list)
    for i in ideas:
        s = i.get("sector") or i.get("cluster_sector") or "Unknown"
        buckets[str(s)].append(i)

    results = []
    for sector_name, bucket in buckets.items():
        stats = compute_slice_stats(bucket)
        stats["name"] = sector_name
        results.append(stats)

    # Sort by avg_r descending
    results.sort(key=lambda s: s.get("avg_r", -99), reverse=True)
    return results


def slice_combined_setup_confluence(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = {
        "high_high": [],
        "high_low": [],
        "low_high": [],
        "low_low": [],
    }
    for i in ideas:
        s = get_setup_score(i)
        c = get_confluence_score(i)
        if s is None or c is None:
            continue
        high_setup = s >= 85
        high_conf = c >= 65
        if high_setup and high_conf:
            buckets["high_high"].append(i)
        elif high_setup and not high_conf:
            buckets["high_low"].append(i)
        elif not high_setup and high_conf:
            buckets["low_high"].append(i)
        else:
            buckets["low_low"].append(i)

    results = []
    for name, bucket in buckets.items():
        stats = compute_slice_stats(bucket)
        stats["name"] = name
        results.append(stats)
    return results


def slice_days_held(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = {
        "held_1_2": [],
        "held_3_5": [],
        "held_6_plus": [],
        "held_unknown": [],
    }
    for i in ideas:
        d = get_days_held(i)
        if d is None:
            buckets["held_unknown"].append(i)
        elif d >= 6:
            buckets["held_6_plus"].append(i)
        elif d >= 3:
            buckets["held_3_5"].append(i)
        else:
            buckets["held_1_2"].append(i)

    results = []
    for name, bucket in buckets.items():
        stats = compute_slice_stats(bucket)
        stats["name"] = name
        results.append(stats)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# INSIGHT GENERATION
# ═══════════════════════════════════════════════════════════════════════════════


def generate_insights(results: dict[str, list[dict[str, Any]]]) -> list[dict[str, str]]:
    insights: list[dict[str, str]] = []

    # Best signal per slice
    for slice_name, slices in results.items():
        valid = [s for s in slices if s.get("count", 0) >= MIN_SAMPLE and s.get("avg_r") is not None]
        if len(valid) < 2:
            continue
        best = max(valid, key=lambda s: s.get("avg_r", -99))
        worst = min(valid, key=lambda s: s.get("avg_r", 99))
        if best["avg_r"] > worst["avg_r"] + 0.3:
            insights.append({
                "type": "signal_edge",
                "signal": slice_name,
                "finding": f"{best['name']} avg {best['avg_r']:.2f}R vs {worst['name']} avg {worst['avg_r']:.2f}R",
                "recommendation": f"Favour {best['name']} setups",
            })

    # Minimum viable confluence threshold
    conf_slices = results.get("confluence_bands", [])
    for s in sorted(conf_slices, key=lambda x: x.get("min", 0)):
        avg_r = s.get("avg_r")
        if avg_r is not None and avg_r > 0 and s.get("count", 0) >= MIN_SAMPLE:
            insights.append({
                "type": "threshold",
                "finding": f"Confluence >= {s['min']} averages positive R ({avg_r:.2f}R, {s['count']} ideas)",
                "recommendation": f"Consider raising confluence minimum to {s['min']}",
            })
            break

    # Gate effectiveness: did STRONG_DOWN ideas underperform?
    chronos = results.get("chronos_direction", [])
    sd = next((s for s in chronos if "strong_down" in s.get("name", "")), None)
    sd_avg = sd.get("avg_r") if sd else None
    if sd and sd.get("count", 0) >= MIN_SAMPLE and sd_avg is not None:
        if sd_avg < 0:
            insights.append({
                "type": "gate_validation",
                "finding": f"STRONG_DOWN ideas avg {sd_avg:.2f}R ({sd['count']} ideas)",
                "recommendation": "STRONG_DOWN gate is working — these ideas lost money",
            })
        elif sd_avg > 0.5:
            insights.append({
                "type": "gate_warning",
                "finding": f"STRONG_DOWN ideas avg {sd_avg:.2f}R ({sd['count']} ideas) — they WOULD have been profitable",
                "recommendation": "STRONG_DOWN gate may be too aggressive — review",
            })

    return insights


# ═══════════════════════════════════════════════════════════════════════════════
# REPORT GENERATION
# ═══════════════════════════════════════════════════════════════════════════════


def fmt(v) -> str:
    if v is None:
        return "-"
    return str(v)


def render_table(rows: list[dict[str, Any]]) -> str:
    lines = ["| Band | Count | Win Rate | Avg R | Median R | Profit Factor | Best | Worst |",
             "|------|-------|----------|-------|----------|---------------|------|-------|"]
    for r in rows:
        if "note" in r:
            lines.append(f"| {r['name']} | {r['count']} | *{r['note']}* | | | | | |")
        else:
            lines.append(
                f"| {r['name']} | {r['count']} | {r['win_rate']}% | {fmt(r['avg_r'])}R | "
                f"{fmt(r['median_r'])}R | {fmt(r['profit_factor'])} | {fmt(r['best_r'])}R | {fmt(r['worst_r'])}R |"
            )
    return "\n".join(lines)


def generate_markdown(results: dict[str, list[dict[str, Any]]], insights: list[dict[str, str]], total: int, date_str: str) -> str:
    md = f"""# SIGNAL VALIDATION REPORT — {date_str}
## Based on {total} resolved ideas

## KEY FINDINGS
"""
    for idx, ins in enumerate(insights[:10], 1):
        md += f"{idx}. **{ins['type'].upper()}**: {ins['finding']}\n"
        md += f"   → *{ins['recommendation']}*\n\n"

    if not insights:
        md += "_No statistically significant insights yet — need more resolved ideas._\n\n"

    md += "## SIGNAL PERFORMANCE TABLE\n\n"

    for title, key in [
        ("Setup Score", "setup_score"),
        ("Confluence Score", "confluence_bands"),
        ("Options Verdict", "options_verdict"),
        ("RVOL Band", "rvol_band"),
        ("Chronos Direction", "chronos_direction"),
        ("Sector Performance", "sector"),
        ("Combined: Setup + Confluence", "combined_setup_confluence"),
        ("Days Held", "days_held"),
    ]:
        md += f"### {title}\n\n"
        md += render_table(results.get(key, []))
        md += "\n\n"

    # Summary section
    md += "## WHAT THIS MEANS\n\n"

    # Best sector
    sectors = results.get("sector", [])
    best_sector = sectors[0] if sectors else None
    if best_sector and best_sector.get("avg_r") is not None:
        md += f"- **Best sector historically**: {best_sector['name']} ({best_sector['avg_r']}R avg)\n"

    # Best combo
    combos = results.get("combined_setup_confluence", [])
    valid_combos = [s for s in combos if s.get("avg_r") is not None]
    best_combo = max(valid_combos, key=lambda s: s.get("avg_r", -99)) if valid_combos else None
    if best_combo:
        md += f"- **Best setup+confluence combo**: {best_combo['name']} ({best_combo['avg_r']}R avg)\n"

    # Weakest signal
    all_signals = []
    for key in ["setup_score", "confluence_bands", "options_verdict", "rvol_band", "chronos_direction"]:
        for s in results.get(key, []):
            if s.get("avg_r") is not None and s.get("count", 0) >= MIN_SAMPLE:
                all_signals.append((key, s["name"], s["avg_r"]))
    if all_signals:
        weakest = min(all_signals, key=lambda x: x[2])
        md += f"- **Weakest signal**: {weakest[1]} in {weakest[0]} ({weakest[2]}R avg)\n"

    # Confluence threshold
    conf = results.get("confluence_bands", [])
    threshold = None
    for s in sorted(conf, key=lambda x: x.get("min", 0)):
        avg_r = s.get("avg_r")
        if avg_r is not None and avg_r > 0 and s.get("count", 0) >= MIN_SAMPLE:
            threshold = s.get("min")
            break
    if threshold is not None:
        md += f"- **Recommended minimum confluence**: {threshold}\n"
    else:
        md += "- **Recommended minimum confluence**: Not yet determined (need more data)\n"

    md += f"\n---\n*Report generated {datetime.now(timezone.utc).isoformat()}*\n"
    return md


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def run_backtest() -> dict[str, Any]:
    ideas = load_fund()

    # Filter to resolved ideas (have some outcome data)
    resolved = [i for i in ideas if get_outcome_r(i) is not None or is_winner(i) is not None]
    total = len(resolved)

    if total == 0:
        print("No resolved ideas found in fund.json")
        return {"total": 0, "results": {}, "insights": []}

    print(f"Resolved ideas found: {total}")

    results = {
        "setup_score": slice_setup_score(resolved),
        "confluence_bands": slice_confluence_score(resolved),
        "options_verdict": slice_options_verdict(resolved),
        "rvol_band": slice_rvol(resolved),
        "chronos_direction": slice_chronos_dir(resolved),
        "sector": slice_sector(resolved),
        "combined_setup_confluence": slice_combined_setup_confluence(resolved),
        "days_held": slice_days_held(resolved),
    }

    insights = generate_insights(results)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Save JSON
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    json_path = DATA_DIR / f"signal_validation_{date_str}.json"
    json.dump(
        {
            "date": date_str,
            "total_ideas": total,
            "results": results,
            "insights": insights,
        },
        json_path.open("w"),
        indent=2,
        default=str,
    )
    print(f"Saved JSON: {json_path}")

    # Save Markdown
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"signal_validation_{date_str}.md"
    md = generate_markdown(results, insights, total, date_str)
    md_path.write_text(md, encoding="utf-8")
    print(f"Saved report: {md_path}")

    return {
        "total": total,
        "results": results,
        "insights": insights,
        "json_path": str(json_path),
        "md_path": str(md_path),
    }


if __name__ == "__main__":
    run_backtest()
