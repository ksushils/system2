#!/usr/bin/env python3
"""
System 2 — Nightly Learning Loop (Prompt 4 of 5).

Self-improvement engine that:
  1. Logs signal snapshots + outcomes to signal_outcomes.jsonl
  2. Computes attribution across all signal slices
  3. Generates nightly post-mortem reports
  4. Generates weekly attribution digests (Sundays)

Usage:
  python3 nightly_learning.py [--postmortem] [--weekly] [--attribution-only]

Cron:
  30 23 * * 1-5  cd /root/system2-core && .venv/bin/python nightly_learning.py
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

from intelligence_engine import (
    track_shadow_performance,
    analyse_stage_funnel,
    analyse_source_value,
    analyse_what_if,
    generate_monthly_report,
    INTELLIGENCE_PATH,
)


ROOT = Path(__file__).resolve().parent
FUND_PATH = Path("/root/fund-system/data/fund.json")
SIGNAL_OUTCOMES_PATH = ROOT / "data" / "signal_outcomes.jsonl"
ATTRIBUTION_PATH = ROOT / "data" / "attribution_latest.json"
REPORTS_DIR = ROOT / "reports"
SHADOW_CACHE_PATH = ROOT / "data" / "shadow_return_cache.json"

FMP_BASE = "https://financialmodelingprep.com"


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
    for key in ("FMP_API_KEY", "FMP_KEY", "TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN",
                "TELEGRAM_CHAT_ID", "TG_CHAT_ID"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def fmp_key() -> str:
    env = load_env()
    return env.get("FMP_API_KEY") or env.get("FMP_KEY") or ""


def send_telegram(text: str) -> dict[str, Any]:
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN") or env.get("TG_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or env.get("TG_CHAT_ID")
    if not token or not chat_id:
        return {"sent": False, "reason": "missing telegram credentials"}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=25,
        )
        return {"sent": r.ok, "status": r.status_code}
    except Exception as e:
        return {"sent": False, "reason": str(e)}


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


def load_fund() -> dict[str, Any]:
    return json.loads(FUND_PATH.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")


# ═══════════════════════════════════════════════════════════════════
# MODULE 1 — SIGNAL OUTCOMES LOGGER
# ═══════════════════════════════════════════════════════════════════

def rvol_bucket(rvol_val: float | None) -> str:
    rv = num(rvol_val)
    if rv is None or rv == 0:
        return "unknown"
    if rv <= 1.5:
        return "low"
    if rv <= 3.0:
        return "mid"
    if rv <= 6.0:
        return "high"
    return "extreme"


def cone_label(band_pct: float | None) -> str:
    bp = num(band_pct)
    if bp is None or bp == 0:
        return "unknown"
    if bp <= 3:
        return "TIGHT"
    if bp <= 6:
        return "MODERATE"
    return "WIDE"


def days_held(idea: dict) -> int | None:
    """Approximate days held from date to paper_exit_at."""
    d1 = idea.get("date")
    d2 = idea.get("paper_exit_at")
    if not d1 or not d2:
        return None
    try:
        dt1 = datetime.strptime(str(d1)[:10], "%Y-%m-%d")
        dt2 = datetime.strptime(str(d2)[:10], "%Y-%m-%d")
        return max(0, (dt2 - dt1).days)
    except Exception:
        return None


def council_quorum_count(idea: dict) -> int:
    count = 0
    for mk in ("gemini", "claude", "kimi", "gpt4o"):
        v = idea.get(f"{mk}_verdict") or idea.get(f"council_{mk}") or ""
        if str(v).strip().upper() not in ("", "ABSTAIN", "-", "NONE", "NULL"):
            count += 1
    return count


def log_resolved_idea(idea: dict[str, Any]) -> dict[str, Any]:
    """Append a resolved idea to signal_outcomes.jsonl. Idempotent by id."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idea_id = idea.get("id")

    # Check if already logged
    existing = load_jsonl(SIGNAL_OUTCOMES_PATH)
    if any(row.get("id") == idea_id for row in existing):
        return {"ok": True, "already_logged": True, "id": idea_id}

    fam = idea.get("family_scores") or {}
    actual_r = num(idea.get("actual_r"))
    trade_r_net = num(idea.get("trade_r_net"))

    record = {
        # Identity
        "id": idea_id,
        "ticker": idea.get("ticker"),
        "date_logged": str(idea.get("date") or idea.get("logged_at", "")[:10]),
        "date_resolved": today,
        "era": idea.get("era", "system2_v2"),
        "set": idea.get("set", 1),
        # Outcome
        "actual_r": actual_r if actual_r != 0 else None,
        "trade_r_net": trade_r_net if trade_r_net != 0 else None,
        "exit_reason": idea.get("paper_exit_reason") or idea.get("hit"),
        "days_held": days_held(idea),
        "hit_tp1": (actual_r or 0) >= 2.5,
        "hit_stop": (actual_r or 0) <= -1.05,
        "mfe_r": idea.get("mfe_r"),
        "mae_r": idea.get("mae_r"),
        "capture_rate": idea.get("capture_rate"),
        "trade_entered": idea.get("trade_entered", False),
        # Signal snapshot — momentum family
        "rvol_value": idea.get("volumeRatio") or idea.get("rvol"),
        "rvol_bucket": rvol_bucket(idea.get("volumeRatio") or idea.get("rvol")),
        "vwap_pct": idea.get("distanceFromVWAP"),
        "rs_vs_spy": idea.get("rsVsSpy") or idea.get("rs_vs_spy"),
        "family_momentum": fam.get("momentum"),
        # Signal snapshot — positioning family
        "options_verdict": idea.get("options_verdict") or idea.get("options_verdict_v2"),
        "options_source": idea.get("options_provider_used"),
        "dark_pool_score": idea.get("dark_pool_signal"),
        "insider_signal": idea.get("insider_buy_signal"),
        "family_positioning": fam.get("positioning"),
        # Signal snapshot — catalyst family
        "danelfin_score": (idea.get("danelfin") or {}).get("ai_score") if isinstance(idea.get("danelfin"), dict) else None,
        "danelfin_available": idea.get("danelfin_data_available", False),
        "squeeze_score": idea.get("short_squeeze_score"),
        "family_catalyst": fam.get("catalyst"),
        # Signal snapshot — structural family
        "gex_regime": idea.get("gex_regime") or idea.get("flashalpha_gex_regime"),
        "seasonality": idea.get("seasonal_signal"),
        "chronos_direction": idea.get("chronos_dir") or idea.get("combined_forecast_dir"),
        "chronos_cone": cone_label(idea.get("chronos_band_pct")),
        "forecast_agreement": idea.get("forecast_agreement", False),
        "family_structural": fam.get("structural"),
        # Scores
        "confluence_score": idea.get("confluence_score"),
        "trade_quality_label": idea.get("trade_quality_label"),
        "data_quality_score": idea.get("data_quality_score"),
        "families_firing": fam.get("families_firing"),
        # Council
        "gemini_verdict": idea.get("gemini_verdict") or idea.get("council_gemini"),
        "claude_verdict": idea.get("claude_verdict") or idea.get("council_claude"),
        "kimi_verdict": idea.get("kimi_verdict") or idea.get("council_kimi"),
        "gpt4o_verdict": idea.get("gpt4o_verdict") or idea.get("council_gpt4o"),
        "council_quorum": council_quorum_count(idea),
        # Market regime at time
        "vix_regime": idea.get("vix_regime") or idea.get("market_regime") or idea.get("regime"),
        "vix_value": idea.get("vix_current"),
        "spy_return_that_day": None,
    }

    append_jsonl(SIGNAL_OUTCOMES_PATH, record)
    return {"ok": True, "already_logged": False, "id": idea_id}


# ═══════════════════════════════════════════════════════════════════
# MODULE 2 — ATTRIBUTION ENGINE
# ═══════════════════════════════════════════════════════════════════

def slice_stats(rows: list[dict]) -> dict[str, Any]:
    """Compute avg_r, win_rate, count, std_r for a list of outcome rows."""
    vals = [num(r.get("actual_r")) for r in rows if r.get("actual_r") is not None]
    if not vals:
        return {"count": len(rows), "avg_r": None, "win_rate": None, "std_r": None}
    wins = sum(1 for v in vals if v > 0)
    avg = sum(vals) / len(vals)
    std = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return {
        "count": len(vals),
        "avg_r": round(avg, 3),
        "win_rate": round(wins / len(vals) * 100, 1),
        "std_r": round(std, 3),
    }


def compute_attribution() -> dict[str, Any]:
    rows = load_jsonl(SIGNAL_OUTCOMES_PATH)
    # Filter to v2 era only
    rows = [r for r in rows if r.get("era") == "system2_v2"]
    total = len(rows)

    if total < 10:
        return {"insufficient_data": True, "count": total, "needed": 10}

    def bucket(name: str, subset: list[dict]) -> dict:
        return {"name": name, **slice_stats(subset)}

    # Helper to collect by condition
    def by(condition_fn) -> list[dict]:
        return [r for r in rows if condition_fn(r)]

    slices = {}

    # SLICE 1 — RVOL bucket
    slices["rvol"] = {
        "rvol_low": bucket("rvol_low", by(lambda r: (r.get("rvol_bucket") or "").lower() in ("low", "1.0-1.5"))),
        "rvol_mid": bucket("rvol_mid", by(lambda r: (r.get("rvol_bucket") or "").lower() in ("mid", "1.5-3.0"))),
        "rvol_high": bucket("rvol_high", by(lambda r: (r.get("rvol_bucket") or "").lower() in ("high", "3.0-6.0"))),
        "rvol_extreme": bucket("rvol_extreme", by(lambda r: (r.get("rvol_bucket") or "").lower() in ("extreme", ">6.0"))),
    }

    # SLICE 2 — Options verdict
    ov = lambda r, v: (r.get("options_verdict") or "").upper() == v
    slices["options"] = {
        "options_strong_confirm": bucket("strong_confirm", by(lambda r: ov(r, "STRONG_CONFIRM"))),
        "options_confirm": bucket("confirm", by(lambda r: ov(r, "CONFIRM"))),
        "options_neutral": bucket("neutral", by(lambda r: ov(r, "NEUTRAL"))),
        "options_no_data": bucket("no_data", by(lambda r: ov(r, "NO_DATA"))),
    }

    # SLICE 3 — Council per model
    for mk in ("gemini", "claude", "kimi", "gpt4o"):
        key = mk + "_verdict"
        slices[mk] = {
            f"{mk}_tier1": bucket(f"{mk}_tier1", by(lambda r, k=key: (r.get(k) or "").upper() in ("TIER1", "UPGRADE"))),
            f"{mk}_tier2": bucket(f"{mk}_tier2", by(lambda r, k=key: (r.get(k) or "").upper() in ("TIER2", "TIER3"))),
            f"{mk}_skip": bucket(f"{mk}_skip", by(lambda r, k=key: (r.get(k) or "").upper() in ("SKIP", "FORCE_SKIP"))),
            f"{mk}_abstain": bucket(f"{mk}_abstain", by(lambda r, k=key: (r.get(k) or "").upper() in ("ABSTAIN", "", "-"))),
        }

    # SLICE 4 — Set
    slices["set"] = {
        "set1": bucket("set1", by(lambda r: r.get("set") == 1 and not r.get("multi_set_idea"))),
        "set2": bucket("set2", by(lambda r: r.get("set") == 2 and not r.get("multi_set_idea"))),
        "multiset": bucket("multiset", by(lambda r: r.get("multi_set_idea") is True)),
    }

    # SLICE 5 — Forecast
    slices["forecast"] = {
        "forecast_agree": bucket("agree", by(lambda r: r.get("forecast_agreement") is True)),
        "forecast_disagree": bucket("disagree", by(lambda r: r.get("forecast_agreement") is False)),
        "forecast_wide_cone": bucket("wide_cone", by(lambda r: (r.get("chronos_cone") or "").upper() == "WIDE")),
        "forecast_tight_cone": bucket("tight_cone", by(lambda r: (r.get("chronos_cone") or "").upper() in ("TIGHT", "MODERATE"))),
    }

    # SLICE 6 — Confluence band
    def conf_band(cs: float | None) -> str:
        cs = num(cs)
        if cs is None or cs == 0:
            return "unknown"
        if cs < 45:
            return "low"
        if cs < 60:
            return "45_60"
        if cs < 75:
            return "60_75"
        return "75_plus"

    slices["confluence"] = {
        "conf_45_60": bucket("45_60", by(lambda r: conf_band(r.get("confluence_score")) == "45_60")),
        "conf_60_75": bucket("60_75", by(lambda r: conf_band(r.get("confluence_score")) == "60_75")),
        "conf_75_plus": bucket("75_plus", by(lambda r: conf_band(r.get("confluence_score")) == "75_plus")),
    }

    # SLICE 7 — Families firing
    ff = lambda r: r.get("families_firing")
    slices["families"] = {
        "single_family": bucket("single", by(lambda r: ff(r) == 1)),
        "two_families": bucket("two", by(lambda r: ff(r) == 2)),
        "three_plus_families": bucket("three_plus", by(lambda r: (ff(r) or 0) >= 3)),
    }

    # SLICE 8 — Data quality
    slices["data_quality"] = {
        "dq_good": bucket("good", by(lambda r: num(r.get("data_quality_score"), 0) >= 70)),
        "dq_poor": bucket("poor", by(lambda r: num(r.get("data_quality_score"), 0) > 0 and num(r.get("data_quality_score"), 0) < 70)),
    }

    # SLICE 9 — Shadow audit (computed separately)
    shadow = compute_shadow_audit()
    slices["shadow"] = shadow

    # Review flags
    review_flags: list[str] = []

    # Council model review
    for mk in ("gemini", "claude", "kimi", "gpt4o"):
        tier1 = slices[mk].get(f"{mk}_tier1", {})
        skip = slices[mk].get(f"{mk}_skip", {})
        if (tier1.get("avg_r") or 0) < -0.3:
            review_flags.append(f"{mk.upper()} TIER1 avg_r {tier1.get('avg_r')}R — model may be miscalibrated")
        if (skip.get("avg_r") or 0) > 0.5:
            review_flags.append(f"{mk.upper()} SKIP avg_r {skip.get('avg_r')}R — model too aggressive")

    # Gate review from shadow
    for gate_name, gate_data in shadow.get("gates", {}).items():
        if gate_data.get("gate_review"):
            review_flags.append(f"Gate '{gate_name}' review: rejected avg +{gate_data.get('avg_return_pct')}% over 5d")

    # Best / weakest signal
    all_signals = []
    for cat, sigs in slices.items():
        if cat == "shadow":
            continue
        for name, stats in sigs.items():
            if stats.get("avg_r") is not None and stats.get("count", 0) >= 5:
                all_signals.append({"category": cat, "name": name, **stats})

    best = max(all_signals, key=lambda x: x.get("avg_r", -999)) if all_signals else None
    weakest = min(all_signals, key=lambda x: x.get("avg_r", 999)) if all_signals else None

    attribution = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "era": "system2_v2",
        "total_resolved": total,
        "slices": slices,
        "review_flags": review_flags,
        "best_signal": best,
        "weakest_signal": weakest,
    }
    save_json(ATTRIBUTION_PATH, attribution)
    return attribution


def compute_shadow_audit() -> dict[str, Any]:
    """Fetch 5-day returns for rejected ideas and audit gate effectiveness."""
    try:
        fund = load_fund()
    except Exception:
        return {"note": "Could not load fund.json", "gates": {}}

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

    api_key = fmp_key()
    gates: dict[str, list[float]] = defaultdict(list)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    recent_rejections = [r for r in rejections if (r.get("date") or "9999-12-31") >= cutoff][:50]
    for rej in recent_rejections:
        ticker = rej.get("ticker")
        date_str = rej.get("date")
        reason = rej.get("reason") or rej.get("stage_rejected") or "unknown"
        if not ticker or not date_str:
            continue

        cache_key = f"{ticker}_{date_str}"
        if cache_key in cache:
            ret = cache[cache_key]
            if ret is not None:
                gates[reason].append(ret)
            continue

        ret = fetch_5d_return(ticker, date_str, api_key)
        cache[cache_key] = ret
        if ret is not None:
            gates[reason].append(ret)

    # Save cache
    save_json(SHADOW_CACHE_PATH, {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": cache,
    })

    audit_gates = {}
    for reason, returns in gates.items():
        if not returns:
            continue
        avg_ret = sum(returns) / len(returns)
        audit_gates[reason] = {
            "count": len(returns),
            "avg_return_pct": round(avg_ret, 2),
            "gate_effective": avg_ret < 0,
            "gate_review": avg_ret > 1.5,
        }

    return {"gates": audit_gates}


def fetch_5d_return(ticker: str, from_date: str, api_key: str) -> float | None:
    """Fetch 5-day return from FMP. Returns percentage or None."""
    if not api_key:
        return None
    try:
        url = f"{FMP_BASE}/stable/historical-price-eod/full?symbol={ticker}&apikey={api_key}"
        r = requests.get(url, timeout=25)
        data = r.json()
        hist = data.get("historical") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not hist:
            return None

        # Find index of from_date
        hist = sorted(hist, key=lambda x: x.get("date", ""))
        idx = None
        for i, bar in enumerate(hist):
            if bar.get("date") == from_date:
                idx = i
                break
        if idx is None:
            return None

        start_bar = hist[idx]
        end_idx = min(idx + 5, len(hist) - 1)
        end_bar = hist[end_idx]
        start_price = num(start_bar.get("close"))
        end_price = num(end_bar.get("close"))
        if start_price and end_price and start_price > 0:
            return round(((end_price - start_price) / start_price) * 100, 2)
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════
# MODULE 3 — NIGHTLY POST-MORTEM
# ═══════════════════════════════════════════════════════════════════

def generate_postmortem(date_str: str | None = None) -> dict[str, Any]:
    today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fund = load_fund()
    ideas = fund.get("ideas", [])
    rejections = fund.get("system2_rejections", [])
    run_meta = fund.get("system2_run_metadata", [])

    # Find latest run metadata
    latest_run = max(run_meta, key=lambda x: x.get("date", "")) if run_meta else {}

    # Resolved today
    resolved_today = [i for i in ideas if i.get("paper_status") in ("CLOSED", "RESOLVED")
                      and str(i.get("paper_exit_at") or i.get("scored_at", "")[:10]) == today]

    # Ideas in play (open from previous nights)
    open_ideas = [i for i in ideas if i.get("paper_status") == "OPEN" and i.get("paper") is not False]

    # Count triggered / gapped past / waiting
    triggered = sum(1 for i in open_ideas if i.get("actual_entry_price") is not None)
    gapped_past = sum(1 for i in open_ideas if i.get("replan_type") == "ADVERSE_GAP")
    waiting = len(open_ideas) - triggered - gapped_past

    # Shadow: best rejected today
    today_rejections = [r for r in rejections if r.get("date") == today]
    shadow = compute_shadow_audit()
    shadow_gates = shadow.get("gates", {})
    best_rejected = None
    worst_rejected = []
    for reason, data in shadow_gates.items():
        if data.get("avg_return_pct") is not None:
            worst_rejected.append({"reason": reason, **data})
    worst_rejected.sort(key=lambda x: x.get("avg_return_pct", 0))
    if worst_rejected:
        best_rejected = worst_rejected[-1]
    worst_three = worst_rejected[:3]

    # Era stats
    era_ideas = [i for i in ideas if i.get("era") == "system2_v2" or i.get("logged_at", "") >= "2026-06-09"]
    era_resolved = [i for i in era_ideas if i.get("paper_status") in ("CLOSED", "RESOLVED")]
    era_trades = [i for i in era_resolved if i.get("trade_entered") is True]
    era_r = [num(i.get("trade_r_net")) for i in era_trades if i.get("trade_r_net") is not None]
    era_wins = sum(1 for r in era_r if r > 0)
    era_avg = sum(era_r) / len(era_r) if era_r else None
    era_pf = None
    if era_r:
        pos = sum(r for r in era_r if r > 0)
        neg = sum(abs(r) for r in era_r if r < 0)
        era_pf = safe_div(pos, neg)
    era_cumulative = sum(era_r) if era_r else 0
    era_dd = 0
    peak = 0
    cum = 0
    for r in sorted(era_r):
        cum += r
        peak = max(peak, cum)
        era_dd = max(era_dd, peak - cum)

    # Attribution
    attr = compute_attribution() if len(load_jsonl(SIGNAL_OUTCOMES_PATH)) >= 10 else {"insufficient_data": True}

    # Data health
    data_sources = [
        ("ImpliedOptions", ROOT / "data" / "options_flow.json"),
        ("Barchart UOA", ROOT / "data" / "barchart_uoa.json"),
        ("StockAnalysis", ROOT / "data" / "stockanalysis_growth.json"),
        ("Danelfin", ROOT / "data" / "danelfin_scores.json"),
        ("FlashAlpha GEX", ROOT / "data" / "flashalpha_gex.json"),
        ("FMP quotes", ROOT / "data" / "universe.json"),
    ]
    data_health = []
    for name, path in data_sources:
        if path.exists():
            try:
                st = path.stat()
                age_h = (datetime.now(timezone.utc) - datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)).total_seconds() / 3600
                status = "✅" if age_h < 26 else "⚠️"
                data_health.append(f"{status} {name}: {round(age_h)}h old")
            except Exception:
                data_health.append(f"❌ {name}: error")
        else:
            data_health.append(f"❌ {name}: missing")

    # Council health
    council_ok = 0
    council_errors = []
    for mk in ("gemini", "claude", "kimi", "gpt4o"):
        v = latest_run.get(f"council_{mk}_ok") if isinstance(latest_run, dict) else None
        if v is True:
            council_ok += 1
        elif v is False:
            council_errors.append(f"{mk}: error")

    # Build markdown
    md_lines = [
        f"# 🌙 SYSTEM 2 POST-MORTEM — {today}",
        "",
        "## TODAY'S PIPELINE",
        f"Run status: {'✅ OK' if latest_run.get('ok') else '❌ FAILED' if latest_run else 'N/A'}",
        f"Finalists generated: {latest_run.get('counts', {}).get('finalists', 'N/A') if isinstance(latest_run, dict) else 'N/A'}",
        f"Regime: {latest_run.get('regime', 'N/A') if isinstance(latest_run, dict) else 'N/A'}",
        "",
        f"## RESOLVED TODAY ({len(resolved_today)} ideas)",
    ]

    for i in resolved_today:
        r_val = num(i.get("actual_r"))
        emoji = "🟢" if (r_val or 0) > 0 else "🔴"
        md_lines.append(f"{emoji} **{i.get('ticker')}** {r_val}R")
        md_lines.append(f"  Exit: {i.get('paper_exit_reason') or i.get('hit')} | Days held: {days_held(i) or 'N/A'}")
        md_lines.append(f"  Signals: RVOL {i.get('volumeRatio') or i.get('rvol') or 'N/A'}x | Options {i.get('options_verdict') or 'N/A'} | Danelfin {(i.get('danelfin') or {}).get('ai_score', 'N/A')}")
        gem = i.get("gemini_verdict") or i.get("council_gemini") or "-"
        cla = i.get("claude_verdict") or i.get("council_claude") or "-"
        kim = i.get("kimi_verdict") or i.get("council_kimi") or "-"
        gpt = i.get("gpt4o_verdict") or i.get("council_gpt4o") or "-"
        md_lines.append(f"  Council: G:{gem} C:{cla} K:{kim} GPT:{gpt}")
        if (i.get("paper_exit_reason") == "STOP" or (r_val or 0) < 0) and any(v.upper() in ("SKIP", "FORCE_SKIP") for v in (gem, cla, kim, gpt)):
            md_lines.append("  Council ✓ — SKIP was correct")
        if (i.get("paper_exit_reason") == "STOP" or (r_val or 0) < 0) and any(v.upper() in ("TIER1", "UPGRADE") for v in (gem, cla, kim, gpt)):
            md_lines.append("  Council ✗ — TIER1 was wrong")
        md_lines.append(f"  MFE: {i.get('mfe_r') or 'N/A'}R | MAE: {i.get('mae_r') or 'N/A'}R | Capture: {i.get('capture_rate') or 'N/A'}%")
        md_lines.append("")

    md_lines.extend([
        "## IDEAS IN PLAY",
        f"Triggered (entered zone): {triggered}/{len(open_ideas)}",
        f"Gapped past zone (never entered): {gapped_past}/{len(open_ideas)}",
        f"Still waiting (zone not reached): {waiting}/{len(open_ideas)}",
        "",
        "## SHADOW WATCH",
    ])

    if best_rejected:
        md_lines.append(f"Best rejected idea avg return: {best_rejected['reason']} +{best_rejected['avg_return_pct']}%")
        if best_rejected["avg_return_pct"] > 2:
            md_lines.append("⚠️ Consider gate review")
    else:
        md_lines.append("No shadow data available yet.")

    if worst_three:
        md_lines.append("Worst rejected gates:")
        for w in worst_three:
            md_lines.append(f"  {w['reason']}: {w['avg_return_pct']}% ({w['count']} ideas)")

    md_lines.extend([
        "",
        "## DATA HEALTH",
    ])
    for dh in data_health:
        md_lines.append(dh)
    md_lines.append(f"Council: {council_ok}/4 models responding")
    if council_errors:
        md_lines.append("Errors: " + ", ".join(council_errors))

    md_lines.extend([
        "",
        "## ERA STATISTICS",
        f"Era: System2 v2 (from 9 Jun 2026)",
        f"Total resolved: {len(era_resolved)} | Win rate: {round(era_wins / len(era_trades) * 100, 1) if era_trades else 0}%",
        f"Avg R (net): {round(era_avg, 2) if era_avg is not None else 'N/A'}R",
        f"Profit factor: {round(era_pf, 2) if era_pf is not None else 'N/A'}",
        f"Current equity: {round(era_cumulative, 2)}R",
        f"Max drawdown: {round(era_dd, 2)}R",
    ])

    if not attr.get("insufficient_data"):
        md_lines.extend([
            "",
            "## ATTRIBUTION",
            f"Best performing signal: {attr.get('best_signal', {}).get('name', 'N/A')} avg {attr.get('best_signal', {}).get('avg_r', 'N/A')}R",
            f"Weakest signal: {attr.get('weakest_signal', {}).get('name', 'N/A')} avg {attr.get('weakest_signal', {}).get('avg_r', 'N/A')}R",
        ])

    if attr.get("review_flags"):
        md_lines.extend(["", "## ⚠️ FLAGS FOR REVIEW"])
        for flag in attr["review_flags"]:
            md_lines.append(f"- {flag}")

    md_lines.extend([
        "",
        "## TOMORROW",
        f"{len(open_ideas)} ideas open | Mode: {latest_run.get('regime', 'N/A') if isinstance(latest_run, dict) else 'N/A'}",
        "Tape starts: 13:25 UTC",
        "Next run: 02:15 UTC",
    ])

    md = "\n".join(md_lines)
    report_path = REPORTS_DIR / f"postmortem_{today}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(md, encoding="utf-8")

    # Telegram short version
    tg_lines = [
        f"🌙 POST-MORTEM {today}",
        f"Resolved: {len(resolved_today)} | Avg: {round(sum(num(i.get('actual_r')) for i in resolved_today) / len(resolved_today), 2) if resolved_today else 'N/A'}R",
        f"Era total: {len(era_resolved)} resolved",
    ]
    for i in resolved_today[:5]:
        r_val = num(i.get("actual_r"))
        emoji = "🟢" if (r_val or 0) > 0 else "🔴"
        tg_lines.append(f"{emoji} {i.get('ticker')} {r_val}R")
    if best_rejected:
        tg_lines.append(f"Shadow: {best_rejected['reason']} +{best_rejected['avg_return_pct']}%")
    tg_lines.append(f"Data: {sum(1 for d in data_health if d.startswith('✅'))}/{len(data_sources)} sources OK | Council: {council_ok}/4 models")
    if attr.get("review_flags"):
        tg_lines.append("⚠️ Flags present — see report")
    tg_lines.append(f"Tomorrow: {len(open_ideas)} open | {latest_run.get('regime', 'N/A') if isinstance(latest_run, dict) else 'N/A'}")

    telegram_text = "\n".join(tg_lines)
    tg_result = send_telegram(telegram_text)

    return {
        "ok": True,
        "date": today,
        "report_path": str(report_path),
        "telegram_sent": tg_result.get("sent"),
        "telegram_status": tg_result.get("status"),
        "resolved_today": len(resolved_today),
        "open_ideas": len(open_ideas),
    }


# ═══════════════════════════════════════════════════════════════════
# MODULE 4 — WEEKLY ATTRIBUTION DIGEST
# ═══════════════════════════════════════════════════════════════════

def generate_weekly_digest() -> dict[str, Any]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    attr = compute_attribution()

    # Run signal backtest for historical validation
    backtest_results: dict[str, Any] = {}
    try:
        import signal_backtest
        backtest_results = signal_backtest.run_backtest()
    except Exception as e:
        print(f"Signal backtest skipped: {e}")

    md_lines = [
        f"# 📊 WEEKLY ATTRIBUTION DIGEST — {today}",
        "",
        f"Total resolved (v2): {attr.get('total_resolved', 'N/A')}",
        "",
        "## ATTRIBUTION TABLE",
    ]

    for cat, sigs in attr.get("slices", {}).items():
        if cat == "shadow":
            continue
        md_lines.append(f"\n### {cat.upper()}")
        for name, stats in sigs.items():
            c = stats.get("count", 0)
            a = stats.get("avg_r")
            w = stats.get("win_rate")
            md_lines.append(f"- {name}: count={c}, avg_r={a}R, win_rate={w}%")

    md_lines.extend(["", "## GATE EFFECTIVENESS"])
    shadow = attr.get("slices", {}).get("shadow", {})
    for gate_name, gate_data in shadow.get("gates", {}).items():
        md_lines.append(f"- {gate_name}: {gate_data.get('count')} rejected, avg {gate_data.get('avg_return_pct')}%")
        if gate_data.get("gate_effective"):
            md_lines.append("  → Gate effective (avg return < 0)")
        if gate_data.get("gate_review"):
            md_lines.append("  ⚠️ Gate review flag (avg return > 1.5%)")

    best = attr.get("best_signal")
    weakest = attr.get("weakest_signal")
    if best:
        md_lines.extend(["", "## TOP 3 SIGNALS"])
        all_sigs = []
        for cat, sigs in attr.get("slices", {}).items():
            if cat == "shadow":
                continue
            for name, stats in sigs.items():
                if stats.get("avg_r") is not None and stats.get("count", 0) >= 5:
                    all_sigs.append({"category": cat, "name": name, **stats})
        all_sigs.sort(key=lambda x: x.get("avg_r", -999), reverse=True)
        for s in all_sigs[:3]:
            md_lines.append(f"- {s['category']}/{s['name']}: {s['avg_r']}R ({s['count']} samples)")
        md_lines.extend(["", "## BOTTOM 3 SIGNALS"])
        for s in all_sigs[-3:]:
            md_lines.append(f"- {s['category']}/{s['name']}: {s['avg_r']}R ({s['count']} samples)")

    # Suggestions
    # Signal backtest findings
    if backtest_results.get("insights"):
        md_lines.extend(["", "## HISTORICAL SIGNAL VALIDATION"])
        md_lines.append(f"Based on {backtest_results.get('total', 0)} resolved ideas")
        for idx, ins in enumerate(backtest_results["insights"][:3], 1):
            md_lines.append(f"{idx}. **{ins['type'].upper()}**: {ins['finding']}")
            md_lines.append(f"   → *{ins['recommendation']}*")
        md_lines.append(f"\nFull report: {backtest_results.get('md_path', 'N/A')}")

    md_lines.extend(["", "## SUGGESTIONS (human approval required)"])
    slices = attr.get("slices", {})

    # RVOL suggestion
    rvol = slices.get("rvol", {})
    rvol_high = rvol.get("rvol_high", {})
    rvol_low = rvol.get("rvol_low", {})
    if rvol_high.get("avg_r") is not None and rvol_low.get("avg_r") is not None:
        diff = rvol_high["avg_r"] - rvol_low["avg_r"]
        if diff > 0.5:
            md_lines.append(f"- **RVOL threshold**: High RVOL (+{rvol_high['avg_r']}R) outperforming low RVOL (+{rvol_low['avg_r']}R) by {round(diff, 2)}R. Consider raising minimum RVOL threshold in b3_surgical_strike.")

    # Gemini suggestion
    gem = slices.get("gemini", {})
    gem_skip = gem.get("gemini_skip", {})
    if gem_skip.get("avg_r") is not None and gem_skip["avg_r"] > 0:
        md_lines.append(f"- **Gemini SKIP**: SKIP ideas averaging +{gem_skip['avg_r']}R — model may be too aggressive. Review Short Seller prompt.")

    # Gate suggestions
    for gate_name, gate_data in shadow.get("gates", {}).items():
        if gate_data.get("gate_review"):
            md_lines.append(f"- **Gate '{gate_name}'**: Rejected ideas averaging +{gate_data['avg_return_pct']}% over 5 days. Consider threshold review.")

    if len(md_lines) == md_lines.index("## SUGGESTIONS (human approval required)") + 1:
        md_lines.append("- No suggestions this week. All signals within expected ranges.")

    md = "\n".join(md_lines)
    report_path = REPORTS_DIR / f"weekly_{today}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(md, encoding="utf-8")

    # Telegram
    tg_lines = [f"📊 WEEKLY DIGEST {today}", f"Resolved: {attr.get('total_resolved', 'N/A')}"]
    if best:
        tg_lines.append(f"Best: {best['category']}/{best['name']} {best['avg_r']}R")
    if weakest:
        tg_lines.append(f"Weakest: {weakest['category']}/{weakest['name']} {weakest['avg_r']}R")
    if backtest_results.get("insights"):
        tg_lines.append("📈 Signal validation:")
        for ins in backtest_results["insights"][:3]:
            tg_lines.append(f"  • {ins['finding']}")
    if attr.get("review_flags"):
        tg_lines.append(f"⚠️ {len(attr['review_flags'])} review flags")
    tg_lines.append("See full report in /root/system2-core/reports/")
    tg_result = send_telegram("\n".join(tg_lines))

    return {
        "ok": True,
        "date": today,
        "report_path": str(report_path),
        "telegram_sent": tg_result.get("sent"),
        "telegram_status": tg_result.get("status"),
    }


# ═══════════════════════════════════════════════════════════════════
# DAILY SCORER WRAPPER
# ═══════════════════════════════════════════════════════════════════

def run_daily_scorer() -> dict[str, Any]:
    """Trigger server scoring, then log any newly resolved ideas."""
    import time
    api_url = os.environ.get("FUND_API_URL", "http://127.0.0.1:3210")

    # 1. Trigger server scoring
    try:
        r = requests.post(f"{api_url}/api/score/run", timeout=120)
        score_result = r.json() if r.ok else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        score_result = {"error": str(e)}

    # Small delay to let server write db
    time.sleep(2)

    # 2. Read fund and log newly resolved ideas
    fund = load_fund()
    ideas = fund.get("ideas", [])
    logged = 0
    for idea in ideas:
        if idea.get("paper_status") in ("CLOSED", "RESOLVED"):
            res = log_resolved_idea(idea)
            if res.get("ok") and not res.get("already_logged"):
                logged += 1

    return {
        "ok": True,
        "scoring": score_result,
        "newly_logged": logged,
    }



# ═══════════════════════════════════════════════════════════════════
# MODULE 5 — COUNCIL CALIBRATION ENGINE
# ═══════════════════════════════════════════════════════════════════

COUNCIL_CALIBRATION_PATH = ROOT / "data" / "council_calibration.json"
COUNCIL_SUGGESTIONS_PATH = ROOT / "data" / "council_suggestions.json"

VERDICT_GROUPS = {
    "tier1": {"UPGRADE", "TIER1"},
    "tier2": {"TIER2", "TIER3"},
    "skip": {"SKIP", "FORCE_SKIP"},
    "abstain": {"ABSTAIN", "", "-", "NONE", "NULL", "ERROR", "TIMEOUT"},
}

MODEL_ORDER = ("gemini", "claude", "kimi", "gpt4o")


def _model_verdict(row: dict, model: str) -> str:
    """Extract a model's verdict from a signal outcome row."""
    v = row.get(f"{model}_verdict") or row.get(f"council_{model}") or ""
    return str(v).strip().upper()


def _group_for_verdict(verdict: str) -> str:
    for group, vals in VERDICT_GROUPS.items():
        if verdict in vals:
            return group
    return "other"


def compute_council_calibration() -> dict[str, Any]:
    """Measure each council model's accuracy against actual outcomes."""
    rows = load_jsonl(SIGNAL_OUTCOMES_PATH)
    # Filter to resolved ideas with actual_r and council verdicts
    resolved = [
        r for r in rows
        if num(r.get("actual_r")) is not None and num(r.get("actual_r")) != 0
    ]
    if not resolved:
        return {"insufficient_data": True, "count": 0, "needed": 5, "models": {}}

    calibration: dict[str, Any] = {}

    for model in MODEL_ORDER:
        model_rows = [
            r for r in resolved
            if _model_verdict(r, model) not in VERDICT_GROUPS["abstain"]
        ]
        if not model_rows:
            calibration[model] = {
                "name": model.capitalize(),
                "total_verdicts": 0,
                "insufficient": True,
            }
            continue

        # Verdict distribution
        dist: dict[str, int] = {}
        for r in model_rows:
            v = _model_verdict(r, model)
            dist[v] = dist.get(v, 0) + 1

        # Per-group stats
        group_stats: dict[str, dict] = {}
        for group_name, group_verdicts in VERDICT_GROUPS.items():
            if group_name == "abstain":
                continue
            subset = [
                r for r in model_rows
                if _model_verdict(r, model) in group_verdicts
            ]
            vals = [num(r.get("actual_r")) for r in subset if num(r.get("actual_r")) is not None]
            group_stats[group_name] = {
                "count": len(subset),
                "avg_r": round(sum(vals) / len(vals), 3) if vals else None,
                "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None,
            }

        # Force-skip rate (% of all verdicts that are FORCE_SKIP)
        force_skip_count = dist.get("FORCE_SKIP", 0)
        total_with_verdict = len(model_rows)
        force_skip_rate = safe_div(force_skip_count, total_with_verdict)

        # Abstain rate (% of all resolved ideas where model abstained)
        abstain_count = sum(
            1 for r in resolved
            if _model_verdict(r, model) in VERDICT_GROUPS["abstain"]
        )
        abstain_rate = safe_div(abstain_count, len(resolved))

        # Agreement rate (% where model agrees with final council verdict)
        agreement_count = 0
        agreement_checked = 0
        for r in resolved:
            mv = _model_verdict(r, model)
            if mv in VERDICT_GROUPS["abstain"]:
                continue
            fv = str(r.get("council_final_verdict") or "").strip().upper()
            # Treat TIER1/UPGRADE as same group, TIER2/TIER3 as same, SKIP/FORCE_SKIP as same
            mv_group = _group_for_verdict(mv)
            fv_group = _group_for_verdict(fv)
            agreement_checked += 1
            if mv_group == fv_group:
                agreement_count += 1
        agreement_rate = safe_div(agreement_count, agreement_checked)

        # Calibration flag
        tier1_avg = group_stats.get("tier1", {}).get("avg_r")
        skip_avg = group_stats.get("skip", {}).get("avg_r")
        tier1_count = group_stats.get("tier1", {}).get("count", 0)
        skip_count = group_stats.get("skip", {}).get("count", 0)

        calibrated = False
        if tier1_count >= 5 and skip_count >= 5:
            calibrated = (
                (tier1_avg or 0) > 0
                and (skip_avg or 0) < 0
                and ((tier1_avg or 0) - (skip_avg or 0)) > 0.5
            )

        calibration[model] = {
            "name": model.capitalize(),
            "total_verdicts": total_with_verdict,
            "distribution": dist,
            "group_stats": group_stats,
            "tier1_avg_r": tier1_avg,
            "skip_avg_r": skip_avg,
            "tier1_count": tier1_count,
            "skip_count": skip_count,
            "force_skip_rate": force_skip_rate,
            "abstain_rate": abstain_rate,
            "agreement_rate": agreement_rate,
            "calibrated": calibrated,
            "insufficient": total_with_verdict < 5,
        }

    result = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "total_resolved": len(resolved),
        "insufficient_data": len(resolved) < 5,
        "models": calibration,
    }
    save_json(COUNCIL_CALIBRATION_PATH, result)
    return result


def generate_council_suggestions(calibration: dict[str, Any]) -> list[dict]:
    """Generate prompt improvement suggestions from calibration data.

    Never auto-modifies prompts — returns suggestions for human review.
    """
    suggestions: list[dict] = []
    if calibration.get("insufficient_data"):
        return suggestions

    for model in MODEL_ORDER:
        stats = calibration.get("models", {}).get(model)
        if not stats or stats.get("insufficient"):
            continue

        # Too aggressive (FORCE_SKIP > 30%)
        fsr = stats.get("force_skip_rate") or 0
        if isinstance(fsr, (int, float)) and fsr > 0.30:
            suggestions.append({
                "model": model,
                "issue": "Too aggressive",
                "finding": f"FORCE_SKIP rate {fsr:.0%} — model skipping {fsr:.0%} of ideas",
                "suggestion": f"Soften {model.capitalize()} persona prompt. Reduce FORCE_SKIP trigger threshold. Reserve FORCE_SKIP for truly dangerous setups only.",
                "prompt_section_to_change": "FORCE_SKIP criteria in persona definition",
            })

        # TIER1 not predicting winners
        tier1_count = stats.get("tier1_count", 0)
        tier1_avg = stats.get("tier1_avg_r")
        if tier1_count >= 5 and tier1_avg is not None and tier1_avg < 0:
            suggestions.append({
                "model": model,
                "issue": "TIER1 miscalibrated",
                "finding": f"{model.capitalize()} TIER1 avg R = {tier1_avg:.2f}R (should be positive)",
                "suggestion": f"Review what triggers {model.capitalize()} TIER1 verdict. Currently TIER1 ideas are losing. The model's bullish criteria may be too loose.",
                "prompt_section_to_change": "TIER1 criteria and scoring rubric",
            })

        # SKIP not predicting losers
        skip_count = stats.get("skip_count", 0)
        skip_avg = stats.get("skip_avg_r")
        if skip_count >= 5 and skip_avg is not None and skip_avg > 0.3:
            suggestions.append({
                "model": model,
                "issue": "SKIP not filtering losers",
                "finding": f"{model.capitalize()} SKIP ideas avg {skip_avg:.2f}R (should be negative)",
                "suggestion": f"{model.capitalize()} is SKIPping winners. The model's bearish criteria may be too aggressive or misaligned with actual risk.",
                "prompt_section_to_change": "SKIP criteria — what triggers avoidance",
            })

        # High abstain rate
        ar = stats.get("abstain_rate") or 0
        if isinstance(ar, (int, float)) and ar > 0.50:
            suggestions.append({
                "model": model,
                "issue": "Not contributing",
                "finding": f"{model.capitalize()} abstaining on {ar:.0%} of ideas",
                "suggestion": f"Check API key validity. If key is valid, the model may be timing out — increase timeout for this model.",
                "prompt_section_to_change": "timeout config",
            })

    save_json(COUNCIL_SUGGESTIONS_PATH, {"computed_at": datetime.now(timezone.utc).isoformat(), "suggestions": suggestions})
    return suggestions


# ═══════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--postmortem", action="store_true", help="Generate nightly post-mortem")
    parser.add_argument("--weekly", action="store_true", help="Generate weekly digest (Sundays)")
    parser.add_argument("--attribution-only", action="store_true", help="Run attribution only")
    parser.add_argument("--daily-scorer", action="store_true", help="Run daily scorer + log resolved ideas")
    parser.add_argument("--intelligence-only", action="store_true", help="Run intelligence engine only")
    parser.add_argument("--date", default=None, help="Date for post-mortem (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.daily_scorer:
        result = run_daily_scorer()
        print(json.dumps(result, indent=2))
        return

    if args.attribution_only:
        result = compute_attribution()
        print(json.dumps(result, indent=2))
        return

    if args.intelligence_only:
        shadow = track_shadow_performance()
        funnel = analyse_stage_funnel()
        sources = analyse_source_value()
        what_if = analyse_what_if()
        intelligence = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "shadow_portfolio": shadow,
            "stage_funnel": funnel,
            "source_value": sources,
            "what_if": what_if,
        }
        INTELLIGENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        INTELLIGENCE_PATH.write_text(json.dumps(intelligence, indent=2, default=str), encoding="utf-8")
        print(json.dumps(intelligence, indent=2, default=str))
        return

    if args.weekly:
        result = generate_weekly_digest()
        print(json.dumps(result, indent=2))
        return

    # Default nightly run: post-mortem + attribution + council calibration + intelligence
    attr = compute_attribution()
    cal = compute_council_calibration()
    sug = generate_council_suggestions(cal)
    pm = generate_postmortem(args.date)

    # Intelligence capture
    shadow = track_shadow_performance()
    funnel = analyse_stage_funnel()
    sources = analyse_source_value()
    what_if = analyse_what_if()
    intelligence = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shadow_portfolio": shadow,
        "stage_funnel": funnel,
        "source_value": sources,
        "what_if": what_if,
    }
    INTELLIGENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    INTELLIGENCE_PATH.write_text(json.dumps(intelligence, indent=2, default=str), encoding="utf-8")

    # Monthly report on 1st of month
    today_day = datetime.now(timezone.utc).day
    if today_day == 1:
        monthly_text = generate_monthly_report()
        send_telegram(f"📊 Monthly Intelligence Report generated\n{monthly_text[:400]}")

    print(json.dumps({"attribution": attr, "council_calibration": cal, "council_suggestions": sug, "postmortem": pm, "intelligence": {"shadow_gates": shadow.get("gates_with_data", 0), "funnel_runs": funnel.get("total_runs", 0), "best_source": sources.get("best_source"), "best_strategy": what_if.get("best_strategy")}}, indent=2, default=str))


if __name__ == "__main__":
    main()
