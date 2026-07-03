#!/usr/bin/env python3
"""System 2 Intelligence Capture Engine.

Produces actionable intelligence about what to keep, remove, improve,
and trust in the System 2 pipeline.

Modules:
  1. Shadow Portfolio Tracker — 5d/10d returns for rejected ideas
  2. Stage Funnel Analysis — pipeline efficiency over time
  3. Source Attribution — which data sources add edge
  4. What-If Analysis — which entry strategies would have worked best

Output: data/intelligence_report.json (consumed by dashboard Intelligence tab)
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════
# PATHS & CONFIG
# ═══════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent
FUND_PATH = Path("/root/fund-system/data/fund.json")
LOGS_DIR = ROOT / "logs"
REPORTS_DIR = ROOT / "reports"
INTELLIGENCE_PATH = ROOT / "data" / "intelligence_report.json"
SHADOW_CACHE_PATH = ROOT / "data" / "shadow_return_cache.json"
FMP_BASE = "https://financialmodelingprep.com"

# ═══════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════


def _log(message: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {message}", flush=True)


def num(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        if math.isfinite(v):
            return v
    except Exception:
        pass
    return default


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def safe_div(a: float, b: float) -> float | None:
    return a / b if b else None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip():
                env[k.strip()] = v.strip().strip("\"'")
    for key in ("FMP_API_KEY", "FMP_KEY"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def fmp_api_key() -> str:
    env = load_env()
    return env.get("FMP_API_KEY") or env.get("FMP_KEY", "")


def load_fund() -> dict[str, Any]:
    return json.loads(FUND_PATH.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def trading_days_since(date_str: str) -> int:
    """Approximate trading days since date (excluding weekends)."""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        days = 0
        cur = d
        while cur < today:
            if cur.weekday() < 5:
                days += 1
            cur += timedelta(days=1)
        return days
    except Exception:
        return 999


def add_trading_days(date_str: str, n: int) -> str:
    """Add N trading days to a date string."""
    d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    added = 0
    cur = d
    while added < n:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur.isoformat()


# ═══════════════════════════════════════════════════════════════════
# FMP PRICE FETCHING
# ═══════════════════════════════════════════════════════════════════


def fetch_fmp_price(ticker: str, date_str: str, api_key: str) -> float | None:
    """Fetch closing price for ticker on date_str from FMP."""
    if not api_key:
        return None
    try:
        url = f"{FMP_BASE}/stable/historical-price-eod/full?symbol={urllib.parse.quote(ticker)}&apikey={api_key}"
        r = urllib.request.urlopen(url, timeout=25)
        data = json.loads(r.read().decode("utf-8", "ignore"))
        hist = data.get("historical") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not hist:
            return None
        hist = sorted(hist, key=lambda x: x.get("date", ""))
        for bar in hist:
            if bar.get("date") == date_str:
                p = num(bar.get("close"))
                if p > 0:
                    return p
        # If exact date not found, try nearest previous trading day
        target = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        for bar in reversed(hist):
            bar_date = datetime.strptime(bar.get("date", "9999-12-31"), "%Y-%m-%d").date()
            if bar_date <= target:
                p = num(bar.get("close"))
                if p > 0:
                    return p
    except Exception as exc:
        _log(f"FMP price fetch {ticker} {date_str}: {exc}")
    return None


# ═══════════════════════════════════════════════════════════════════
# MODULE 1 — SHADOW PORTFOLIO TRACKER
# ═══════════════════════════════════════════════════════════════════


def track_shadow_performance() -> dict[str, Any]:
    """Fetch 5-day and 10-day returns for rejected ideas and audit gate effectiveness."""
    try:
        fund = load_fund()
    except Exception as exc:
        return {"error": f"Could not load fund.json: {exc}", "gates": {}}

    rejections = fund.get("system2_rejections", [])
    if not rejections:
        return {"note": "No rejections in fund.json", "gates": {}}

    # Load cache
    cache: dict[str, Any] = {}
    if SHADOW_CACHE_PATH.exists():
        try:
            raw = json.loads(SHADOW_CACHE_PATH.read_text(encoding="utf-8"))
            cache = raw.get("entries", raw) if isinstance(raw, dict) else {}
        except Exception:
            cache = {}

    api_key = fmp_api_key()
    gates: dict[str, list[float]] = defaultdict(list)

    tracked_count = 0
    skipped_count = 0
    errors_count = 0

    # Only process rejections with price_scoring_eligible=True or those that look like they had a chance
    eligible = [r for r in rejections if r.get("price_scoring_eligible") or True]

    for rej in eligible:
        ticker = rej.get("ticker")
        date_str = rej.get("date")
        reason = rej.get("reason") or rej.get("stage_rejected") or "unknown"

        if not ticker or not date_str:
            skipped_count += 1
            continue

        cache_key = f"{ticker}_{date_str}"

        # Skip if already have returns
        if cache_key in cache and cache[cache_key] is not None:
            ret = cache[cache_key]
            gates[reason].append(ret)
            tracked_count += 1
            continue

        # Skip if rejection was < 5 trading days ago
        tds = trading_days_since(date_str)
        if tds < 5:
            skipped_count += 1
            continue

        try:
            price_at = fetch_fmp_price(ticker, date_str, api_key)
            date_5d = add_trading_days(date_str, 5)
            price_5d = fetch_fmp_price(ticker, date_5d, api_key)
            date_10d = add_trading_days(date_str, 10)
            price_10d = fetch_fmp_price(ticker, date_10d, api_key)

            if price_at and price_5d:
                ret_5d = ((price_5d - price_at) / price_at) * 100
                ret_10d = ((price_10d - price_at) / price_at) * 100 if price_10d else None

                cache[cache_key] = {
                    "shadow_5d_return": round(ret_5d, 2),
                    "shadow_10d_return": round(ret_10d, 2) if ret_10d is not None else None,
                    "shadow_tracked_at": now_iso(),
                }
                gates[reason].append(ret_5d)
                tracked_count += 1
            else:
                cache[cache_key] = None
                errors_count += 1

        except Exception as exc:
            _log(f"Shadow track {ticker} {date_str}: {exc}")
            errors_count += 1
            continue

    # Save cache
    save_json(SHADOW_CACHE_PATH, {
        "generated_at": now_iso(),
        "entries": cache,
    })

    # Build gate summary
    summary = {}
    for gate, returns in gates.items():
        if len(returns) >= 3:
            avg_ret = mean(returns)
            summary[gate] = {
                "count": len(returns),
                "avg_5d_return": round(avg_ret, 2),
                "pct_went_up": round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1),
                "best": round(max(returns), 2),
                "worst": round(min(returns), 2),
                "gate_effective": avg_ret < 0,
                "review_flag": avg_ret > 1.5,
                "status": "✅ Working" if avg_ret < 0 else ("⚠️ Review" if avg_ret > 1.5 else "⚠️ Marginal"),
            }

    # Overall stats
    all_returns = [r for g in gates.values() for r in g]

    return {
        "tracked_count": tracked_count,
        "skipped_count": skipped_count,
        "errors_count": errors_count,
        "total_rejections": len(rejections),
        "gates_with_data": len(summary),
        "overall_avg_5d_return": round(mean(all_returns), 2) if all_returns else None,
        "overall_pct_up": round(sum(1 for r in all_returns if r > 0) / len(all_returns) * 100, 1) if all_returns else None,
        "gates": summary,
        "gates_effective": sum(1 for g in summary.values() if g["gate_effective"]),
        "gates_to_review": sum(1 for g in summary.values() if g["review_flag"]),
        "generated_at": now_iso(),
    }


# ═══════════════════════════════════════════════════════════════════
# MODULE 2 — STAGE FUNNEL ANALYSIS
# ═══════════════════════════════════════════════════════════════════


def analyse_stage_funnel() -> dict[str, Any]:
    """Analyze pipeline efficiency from run metadata."""
    fund = load_fund()
    run_meta = fund.get("system2_run_metadata", [])

    def runtime_seconds(run: dict[str, Any]) -> float:
        for key in ("totalRuntimeSeconds", "runtime_seconds", "runtimeSeconds"):
            try:
                value = float(run.get(key) or 0)
                if value > 0:
                    return value
            except Exception:
                pass
        started = run.get("runStartedAt") or run.get("started_at") or run.get("startedAt")
        finished = run.get("runFinishedAt") or run.get("finished_at") or run.get("finishedAt")
        if started and finished:
            try:
                start_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                finish_dt = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
                seconds = (finish_dt - start_dt).total_seconds()
                return seconds if seconds > 0 else 0
            except Exception:
                return 0
        return 0

    # Also check log files
    log_files = sorted(LOGS_DIR.glob("phase_b_core_*.json")) if LOGS_DIR.exists() else []
    log_runs = []
    for f in log_files[-30:]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            log_runs.append(data)
        except Exception:
            continue

    # Prefer structured run_metadata from fund.json; fall back to log files
    runs = []
    if run_meta:
        for r in run_meta[-30:]:
            counts = r.get("counts", {})
            stages = r.get("stages", [])
            date_key = str(r.get("date", ""))[:10]
            matching_log = next((
                lr for lr in reversed(log_runs)
                if str(lr.get("runStartedAt") or lr.get("runFinishedAt") or "")[:10] == date_key
            ), {})
            runs.append({
                "date": date_key,
                "ok": bool(matching_log.get("ok", True)),
                "universe": counts.get("universe", 0),
                "stage1": counts.get("stage1", 0),
                "stage2": counts.get("stage2", 0),
                "finalists": counts.get("finalists", 0),
                "runtime": runtime_seconds(r) or runtime_seconds(matching_log),
                "regime": "UNKNOWN",
                "failed_at": matching_log.get("failed_at_step"),
            })
    elif log_runs:
        for r in log_runs[-30:]:
            runs.append({
                "date": str(r.get("run_id", ""))[:8],
                "ok": bool(r.get("ok")),
                "universe": r.get("universeCount", 0),
                "stage1": r.get("stage1SurvivorCount", 0),
                "stage2": r.get("stage2TopCount", 0),
                "finalists": r.get("stage7FinalistCount") or r.get("finalists", 0),
                "runtime": runtime_seconds(r),
                "regime": r.get("regime_check", {}).get("regime", "UNKNOWN"),
                "failed_at": r.get("failed_at_step"),
            })

    if not runs:
        return {"error": "no run data found", "total_runs": 0}

    successful = [r for r in runs if r["ok"]]
    failures = [r for r in runs if not r["ok"]]

    def calc_rate(numer: list[dict], denom_key: str, numer_key: str) -> float:
        vals = []
        for r in numer:
            d = r.get(denom_key, 0)
            n = r.get(numer_key, 0)
            if d and d > 0:
                vals.append(n / d)
        return mean(vals) if vals else 0.0

    # Funnel ratios
    stage1_rate = calc_rate(successful, "universe", "stage1")
    stage2_rate = calc_rate(successful, "stage1", "stage2")
    finalist_rate = calc_rate(successful, "stage2", "finalists")

    # Failure analysis
    failure_steps: dict[str, int] = defaultdict(int)
    for f in failures:
        step = f.get("failed_at") or "unknown"
        failure_steps[step] += 1

    # Regime distribution
    regime_counts: dict[str, int] = defaultdict(int)
    for r in successful:
        reg = r.get("regime", "UNKNOWN")
        regime_counts[reg] += 1

    # Finalist trend (last 14 successful)
    finalist_trend = [
        {"date": r["date"], "count": r.get("finalists", 0)}
        for r in successful[-14:]
    ]

    # Runtime trend
    runtimes = [r.get("runtime", 0) / 60 for r in successful if r.get("runtime")]
    runtime_trend = "stable"
    if len(runtimes) >= 3:
        first_half = mean(runtimes[: len(runtimes) // 2])
        second_half = mean(runtimes[len(runtimes) // 2 :])
        if second_half < first_half * 0.85:
            runtime_trend = "improving"
        elif second_half > first_half * 1.15:
            runtime_trend = "degrading"

    return {
        "total_runs": len(runs),
        "successful_runs": len(successful),
        "failed_runs": len(failures),
        "success_rate": round(len(successful) / len(runs) * 100, 1) if runs else 0,
        "avg_universe": round(mean([r["universe"] for r in successful])) if successful else 0,
        "avg_stage1_survivors": round(mean([r["stage1"] for r in successful])) if successful else 0,
        "avg_stage2_top": round(mean([r["stage2"] for r in successful])) if successful else 0,
        "avg_finalists": round(mean([r["finalists"] for r in successful])) if successful else 0,
        "stage1_pass_rate": round(stage1_rate * 100, 1),
        "stage2_pass_rate": round(stage2_rate * 100, 1),
        "finalist_pass_rate": round(finalist_rate * 100, 1),
        "failure_by_step": dict(failure_steps),
        "regime_distribution": dict(regime_counts),
        "finalist_trend": finalist_trend,
        "runtime_trend": runtime_trend,
        "avg_runtime_minutes": round(mean(runtimes), 1) if runtimes else 0,
        "generated_at": now_iso(),
    }


# ═══════════════════════════════════════════════════════════════════
# MODULE 3 — SOURCE ATTRIBUTION
# ═══════════════════════════════════════════════════════════════════


def analyse_source_value() -> dict[str, Any]:
    """Compare resolved idea performance when each data source had data vs didn't."""
    fund = load_fund()
    ideas = fund.get("ideas", [])
    resolved = [i for i in ideas if i.get("actual_r") is not None]

    if len(resolved) < 5:
        return {
            "insufficient_data": True,
            "count": len(resolved),
            "needed": 5,
            "source_performance": {},
            "ranked_by_value": [],
            "best_source": None,
            "weakest_source": None,
        }

    # Define source detectors — gracefully handles missing fields
    sources = {
        "options_flow_present": {
            "field": "options_provider_used",
            "has_value": lambda v: v is not None and v != "" and v != "NONE",
        },
        "chronos_present": {
            "field": "chronos_dir",
            "has_value": lambda v: v is not None and v != "" and v != "FLAT",
        },
        "council_present": {
            "field": "council_votes",
            "has_value": lambda v: v is not None,
        },
        "pre_market_gap_checked": {
            "field": "pre_market_checked_at",
            "has_value": lambda v: v is not None and v != "",
        },
        "strong_council_confidence": {
            "field": "council_conf",
            "has_value": lambda v: num(v, 0) >= 0.7,
        },
    }

    results: dict[str, Any] = {}
    for source_name, config in sources.items():
        with_source = [i for i in resolved if config["has_value"](i.get(config["field"]))]
        without_source = [i for i in resolved if not config["has_value"](i.get(config["field"]))]

        if len(with_source) >= 3:
            with_r = [i["actual_r"] for i in with_source]
            without_r = [i["actual_r"] for i in without_source] if without_source else []
            results[source_name] = {
                "with_count": len(with_source),
                "with_avg_r": round(mean(with_r), 2),
                "with_win_rate": round(sum(1 for r in with_r if r > 0) / len(with_r) * 100, 1),
                "without_count": len(without_source),
                "without_avg_r": round(mean(without_r), 2) if without_r else None,
                "value_add": None,
            }
            if without_r:
                results[source_name]["value_add"] = round(
                    results[source_name]["with_avg_r"] - results[source_name]["without_avg_r"], 2
                )

    # Rank sources by value add
    ranked = sorted(
        [(k, v) for k, v in results.items() if v.get("value_add") is not None],
        key=lambda x: x[1]["value_add"],
        reverse=True,
    )

    return {
        "insufficient_data": False,
        "count": len(resolved),
        "source_performance": results,
        "ranked_by_value": [r[0] for r in ranked],
        "best_source": ranked[0][0] if ranked else None,
        "weakest_source": ranked[-1][0] if ranked else None,
        "generated_at": now_iso(),
    }


# ═══════════════════════════════════════════════════════════════════
# MODULE 4 — WHAT-IF ANALYSIS
# ═══════════════════════════════════════════════════════════════════


def analyse_what_if() -> dict[str, Any]:
    """Simulate different entry strategies on historical resolved ideas."""
    fund = load_fund()
    ideas = fund.get("ideas", [])

    # Prefer actual_r, fall back to paper_exit_r
    def get_r(i: dict) -> float | None:
        return i.get("actual_r") if i.get("actual_r") is not None else i.get("paper_exit_r")

    with_r = [i for i in ideas if get_r(i) is not None]

    if len(with_r) < 5:
        return {
            "insufficient_data": True,
            "count": len(with_r),
            "needed": 5,
            "strategies": {},
            "best_strategy": None,
            "recommendation": "insufficient data",
        }

    strategies: dict[str, list[dict]] = {
        "enter_all": with_r,
        "council_tier1_only": [
            i for i in with_r
            if str(i.get("council_tier") or "").upper() in ("TIER1", "UPGRADE")
        ],
        "council_confident": [
            i for i in with_r
            if num(i.get("council_conf"), 0) >= 0.7
        ],
        "chronos_agreeing": [
            i for i in with_r
            if str(i.get("chronos_dir") or "").upper() in ("UP", "STRONG_UP")
        ],
        "pre_market_favourable": [
            i for i in with_r
            if i.get("pre_market_gap_favourable") == True
        ],
        "mfe_above_5pct": [
            i for i in with_r
            if num(i.get("mfe_pct"), 0) >= 5
        ],
    }

    results: dict[str, Any] = {}
    for strategy, subset in strategies.items():
        if len(subset) >= 3:
            returns = [get_r(i) for i in subset]
            results[strategy] = {
                "count": len(subset),
                "avg_r": round(mean(returns), 2),
                "win_rate": round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1),
                "total_r": round(sum(returns), 2),
            }

    # Best strategy by avg R
    best = None
    if results:
        best = max(results.items(), key=lambda x: x[1].get("avg_r", -99))

    return {
        "insufficient_data": False,
        "count": len(with_r),
        "strategies": results,
        "best_strategy": best[0] if best else None,
        "recommendation": (
            f"Based on {len(with_r)} resolved ideas, '{best[0]}' produced the best avg R "
            f"({best[1]['avg_r']}R) from {best[1]['count']} trades"
            if best else "insufficient data"
        ),
        "generated_at": now_iso(),
    }


# ═══════════════════════════════════════════════════════════════════
# MODULE 5 — MONTHLY REPORT GENERATOR
# ═══════════════════════════════════════════════════════════════════


def generate_monthly_report(month: str | None = None) -> str:
    """Generate a markdown monthly review report."""
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")

    fund = load_fund()
    ideas = fund.get("ideas", [])
    run_meta = fund.get("system2_run_metadata", [])
    rejections = fund.get("system2_rejections", [])

    # Filter to month
    month_ideas = [i for i in ideas if str(i.get("date", "")).startswith(month)]
    month_runs = [r for r in run_meta if str(r.get("date", "")).startswith(month)]
    month_rejections = [r for r in rejections if str(r.get("date", "")).startswith(month)]

    resolved = [i for i in month_ideas if i.get("actual_r") is not None]
    successful_runs = [r for r in month_runs]  # run_meta doesn't have ok flag

    # Source attribution for month
    source_attr = analyse_source_value()
    what_if = analyse_what_if()

    # Shadow from cache if available
    shadow = {}
    if SHADOW_CACHE_PATH.exists():
        try:
            raw = json.loads(SHADOW_CACHE_PATH.read_text(encoding="utf-8"))
            shadow = raw.get("entries", raw) if isinstance(raw, dict) else {}
        except Exception:
            pass

    lines = [
        f"# SYSTEM 2 MONTHLY REVIEW — {month}",
        "",
        "## PIPELINE RELIABILITY",
        f"  Runs this month: {len(month_runs)} | Successful: {len(successful_runs)} | Avg finalists/night: {round(mean([r.get('counts',{}).get('finalists',0) for r in month_runs])) if month_runs else 0}",
        "",
        "## SIGNAL PERFORMANCE",
        f"  Resolved trades: {len(resolved)}",
        f"  Avg R: {round(mean([i['actual_r'] for i in resolved]), 2) if resolved else 'N/A'}",
        f"  Win rate: {round(sum(1 for i in resolved if i['actual_r'] > 0) / len(resolved) * 100, 1) if resolved else 0}%",
        "",
        "## SOURCE VALUE",
        f"  Best source: {source_attr.get('best_source') or 'N/A'}",
        f"  Weakest: {source_attr.get('weakest_source') or 'N/A'}",
        "",
        "## STRATEGY PERFORMANCE",
        f"  {what_if.get('recommendation', 'N/A')}",
        "",
        "## COUNCIL PERFORMANCE",
        "  Models calibrated: see council_calibration.json",
        "",
        "## RECOMMENDATIONS FOR NEXT MONTH",
        "  1. Continue accumulating resolved idea data for richer attribution",
        "  2. Review shadow portfolio gates flagged for review",
        "  3. Validate best-performing strategy with larger sample",
        "",
        f"_Report generated at {now_iso()}_",
    ]

    report_text = "\n".join(lines)
    report_path = REPORTS_DIR / f"monthly_{month}.md"
    save_json(report_path, report_text)
    return report_text


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════


def run_all() -> dict[str, Any]:
    """Run all intelligence modules and save combined report."""
    _log("Intelligence Engine starting")

    shadow = track_shadow_performance()
    _log(f"Shadow: {shadow.get('tracked_count', 0)} tracked, {shadow.get('gates_with_data', 0)} gates")

    funnel = analyse_stage_funnel()
    _log(f"Funnel: {funnel.get('total_runs', 0)} runs, {funnel.get('success_rate', 0)}% success")

    sources = analyse_source_value()
    _log(f"Sources: {sources.get('count', 0)} resolved ideas, best={sources.get('best_source')}")

    what_if = analyse_what_if()
    _log(f"What-if: {what_if.get('count', 0)} ideas, best strategy={what_if.get('best_strategy')}")

    intelligence = {
        "generated_at": now_iso(),
        "shadow_portfolio": shadow,
        "stage_funnel": funnel,
        "source_value": sources,
        "what_if": what_if,
    }

    save_json(INTELLIGENCE_PATH, intelligence)
    _log(f"Saved intelligence report to {INTELLIGENCE_PATH}")
    return intelligence


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-only", action="store_true")
    parser.add_argument("--funnel-only", action="store_true")
    parser.add_argument("--sources-only", action="store_true")
    parser.add_argument("--what-if-only", action="store_true")
    parser.add_argument("--monthly", default=None, help="Generate monthly report (YYYY-MM)")
    args = parser.parse_args()

    if args.monthly:
        text = generate_monthly_report(args.monthly)
        print(text)
        sys.exit(0)

    if args.shadow_only:
        print(json.dumps(track_shadow_performance(), indent=2, default=str))
    elif args.funnel_only:
        print(json.dumps(analyse_stage_funnel(), indent=2, default=str))
    elif args.sources_only:
        print(json.dumps(analyse_source_value(), indent=2, default=str))
    elif args.what_if_only:
        print(json.dumps(analyse_what_if(), indent=2, default=str))
    else:
        print(json.dumps(run_all(), indent=2, default=str))
