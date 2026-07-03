#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from build_council_v51 import SYSTEM_PROMPT, USER_TEMPLATE


ROOT = Path(__file__).resolve().parent
FUND_URL = "http://127.0.0.1:3210/api/idea"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_env() -> None:
    for path in [ROOT / ".env", Path("/var/www/localrank-ai-pro/.env"), Path("/docker/n8n/.env")]:
        load_env_file(path)


def first(*vals, default="-"):
    for val in vals:
        if val not in (None, ""):
            return val
    return default


def render(template: str, values: dict[str, Any]) -> str:
    def repl(match):
        return str(values.get(match.group(1).strip(), "-"))
    return re.sub(r"{{\s*([a-zA-Z0-9_]+)\s*}}", repl, template)


def values_for(row: dict[str, Any]) -> dict[str, Any]:
    entry_zone = first(row.get("entryZone"), row.get("entry_zone"), default=[])
    entry_low = entry_zone[0] if isinstance(entry_zone, list) and entry_zone else first(row.get("entry"), row.get("price"))
    entry_high = entry_zone[1] if isinstance(entry_zone, list) and len(entry_zone) > 1 else entry_low
    return {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "ticker": first(row.get("ticker"), row.get("symbol")),
        "sector": first(row.get("sector")),
        "setup_type": first(row.get("setup_type"), row.get("setupType"), row.get("setup")),
        "grade": first(row.get("grade")),
        "setup_score": first(row.get("setupQualityScore"), row.get("setup_score"), row.get("convictionScore")),
        "confluence_score": first(row.get("confluence_score"), row.get("confluenceScore")),
        "rvol": first(row.get("volumeRatio"), row.get("rvol")),
        "rs_vs_spy": first(row.get("rsVsSpy"), row.get("rs_vs_spy")),
        "vwap_pct": first(row.get("distanceFromVWAP"), row.get("vwap_distance")),
        "atr_daily": first(row.get("atr_daily"), row.get("atr14"), row.get("atr")),
        "sector_rs": first(row.get("sectorAlpha"), row.get("sector_rs")),
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": first(row.get("stopLoss"), row.get("stop")),
        "stop_atr_multiple": first(row.get("stop_atr_multiple"), row.get("stopAtrMultiple")),
        "tp1": first(row.get("tp1"), row.get("target")),
        "tp2": first(row.get("tp2")),
        "rr": first(row.get("rewardRisk"), row.get("rr")),
        "shares": first(row.get("positionShares"), (row.get("cluster") or {}).get("shares")),
        "risk_dollars": first(row.get("positionRiskDollars"), (row.get("cluster") or {}).get("actualRiskDollars")),
        "options_verdict": first(row.get("options_verdict")),
        "options_signals_count": first(row.get("options_signals_count"), 0),
        "iv_rank": first(row.get("iv_rank"), row.get("iv_rank_proxy")),
        "call_vol_oi_ratio": first(row.get("call_vol_oi_ratio"), row.get("vol_oi_ratio")),
        "put_call_vol_ratio": first(row.get("put_call_vol_ratio")),
        "chronos_dir": first(row.get("chronos_dir"), row.get("chronos_direction"), row.get("forecastDecision")),
        "chronos_conviction": first(row.get("forecastConviction"), row.get("chronos_conf")),
        "chronos_band_pct": first(row.get("chronos_band_pct")),
        "catalyst_summary": first(row.get("catalyst_summary")),
        "sub_type": first(row.get("sub_type")),
        "analyst_change": json.dumps(first(row.get("analyst_change"))),
        "seasonality_score": first(row.get("seasonality_score")),
        "dark_pool_elevated": first(row.get("dark_pool_elevated")),
        "regime": first(row.get("regime"), row.get("market_regime")),
        "spy_1d_pct": first(row.get("spy_1d_pct")),
        "qqq_1d_pct": first(row.get("qqq_1d_pct")),
        "vix_current": first(row.get("vix_current")),
        "sector_etf_pct": first(row.get("sector_etf_pct")),
    }


def parse_model_json(text: str | None, ticker: str) -> dict[str, Any]:
    fallback = {
        "ticker": ticker,
        "verdict": "CLEAR",
        "confidence": 50,
        "upgrade_signals": [],
        "red_flags": ["parse_error"],
        "reason": "Model returned unparseable response",
        "force_skip": False,
        "size_view": "full",
    }
    if not text:
        return {**fallback, "reason": "Model returned empty response"}
    try:
        clean = re.sub(r"```json|```", "", text).strip()
        match = re.search(r"{[\s\S]*}", clean)
        parsed = json.loads(match.group(0) if match else clean)
    except Exception:
        return fallback
    parsed["ticker"] = ticker
    parsed["confidence"] = max(0, min(100, int(round(float(parsed.get("confidence", 50))))))
    parsed["upgrade_signals"] = parsed.get("upgrade_signals") if isinstance(parsed.get("upgrade_signals"), list) else []
    parsed["red_flags"] = parsed.get("red_flags") if isinstance(parsed.get("red_flags"), list) else []
    parsed["force_skip"] = bool(parsed.get("force_skip")) or parsed.get("verdict") == "FORCE_SKIP"
    parsed["size_view"] = parsed.get("size_view") or "full"
    return parsed


PERSONAS = {
    "claude": "You are the BALANCED REVIEWER. Look for both risks and genuine strengths with equal weight.",
    "gpt": "You are the MOMENTUM CHASER. Focus on whether trend, volume, and catalyst can sustain a 2-10 day move. What stops momentum?",
    "gemini": "You are the CYNICAL SHORT SELLER. Find every reason this trade fails and risks others missed. Look hard before saying CLEAR.",
}


def call_claude(system: str, user: str) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        json={"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"), "temperature": 0, "max_tokens": 500, "system": system, "messages": [{"role": "user", "content": user}]},
        timeout=60,
    )
    r.raise_for_status()
    return "\n".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def call_gpt(system: str, user: str) -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": "gpt-4o", "temperature": 0, "max_tokens": 500, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def call_gemini(system: str, user: str) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}",
        headers={"Content-Type": "application/json"},
        json={"system_instruction": {"parts": [{"text": system}]}, "contents": [{"parts": [{"text": user}]}], "generationConfig": {"temperature": 0, "maxOutputTokens": 500}},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def merge_verdict(models: list[dict[str, Any]]) -> dict[str, Any]:
    models_available = sum(
        "claude_unavailable" not in (m.get("red_flags") or [])
        and "parse_error" not in (m.get("red_flags") or [])
        for m in models
    )
    any_force_skip = any(m.get("force_skip") is True for m in models)
    if any_force_skip:
        skipper = next(m for m in models if m.get("force_skip"))
        return {
            "ticker": models[0]["ticker"], "council_tier": "FORCE_SKIP", "size_multiplier": 0,
            "yes_count": 0, "avg_confidence": 0, "upgrade_signals": [], "all_red_flags": ["force_skip"],
            "council_claude": models[0]["verdict"], "council_gpt": models[1]["verdict"], "council_gemini": models[2]["verdict"],
            "council_reasons": " | ".join(m.get("reason", "") for m in models), "council_force_skip": True,
            "force_skip_reason": skipper.get("reason"), "remove_from_list": True,
            "models_available": models_available,
        }
    strong = sum(m.get("verdict") == "STRONG" for m in models)
    clear = sum(m.get("verdict") == "CLEAR" for m in models)
    weak = sum(m.get("verdict") == "WEAK" for m in models)
    skip = sum(m.get("verdict") == "SKIP" for m in models)
    positive = strong + clear
    avg_conf = round(sum(int(m.get("confidence", 50)) for m in models) / 3)
    if strong == 3 and avg_conf >= 75:
        tier, size = "UPGRADE", 1.25
    elif positive == 3 and avg_conf >= 75:
        tier, size = "TIER1", 1.0
    elif positive == 3 and avg_conf >= 55:
        tier, size = "TIER2", 0.75
    elif positive == 2 and avg_conf >= 70:
        tier, size = "TIER2", 0.75
    elif positive == 2 and avg_conf < 70:
        tier, size = "TIER3", 0.5
    elif skip >= 2 or (skip == 1 and weak == 2):
        tier, size = "SKIP", 0
    elif weak >= 2:
        tier, size = "TIER3", 0.5
    else:
        tier, size = "TIER3", 0.5
    return {
        "ticker": models[0]["ticker"], "council_tier": tier, "size_multiplier": size,
        "yes_count": positive, "avg_confidence": avg_conf,
        "upgrade_signals": sorted({x for m in models for x in m.get("upgrade_signals", [])}),
        "all_red_flags": sorted({x for m in models for x in m.get("red_flags", [])}),
        "council_claude": models[0]["verdict"], "council_gpt": models[1]["verdict"], "council_gemini": models[2]["verdict"],
        "council_claude_conf": models[0]["confidence"], "council_gpt_conf": models[1]["confidence"], "council_gemini_conf": models[2]["confidence"],
        "council_reasons": " | ".join(m.get("reason", "") for m in models),
        "council_force_skip": False, "remove_from_list": False,
        "models_available": models_available,
    }


def latest_finalists() -> list[dict[str, Any]]:
    for name in ["stage7_clustered_survivors.json", "stage3_news_safe_top40.json", "stage2_surgical_strike_top40.json"]:
        path = ROOT / name
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
            if rows:
                return rows
    raise RuntimeError("No finalist artifact found")


def post_idea(row: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    vals = values_for(row)
    payload = {
        "date": vals["date"], "ticker": vals["ticker"], "mode": "SWING", "paper": True,
        "source": first(row.get("source"), "scanner"), "entry": vals["entry_low"], "stop": vals["stop"], "target": vals["tp1"],
        "sector": vals["sector"], "setup": vals["setup_type"], "grade": vals["grade"],
        "setup_score": vals["setup_score"], "confluence_score": vals["confluence_score"],
        "council_tier": verdict["council_tier"], "council_votes": verdict["yes_count"], "council_conf": verdict["avg_confidence"],
        "council_size_mult": verdict["size_multiplier"], "council_upgrade_sigs": verdict["upgrade_signals"], "council_red_flags": verdict["all_red_flags"],
        "council_claude": verdict["council_claude"], "council_gpt": verdict["council_gpt"], "council_gemini": verdict["council_gemini"],
        "council_claude_conf": verdict.get("council_claude_conf"), "council_gpt_conf": verdict.get("council_gpt_conf"), "council_gemini_conf": verdict.get("council_gemini_conf"),
        "council_reasons": verdict["council_reasons"], "council_force_skip": verdict["council_force_skip"],
    }
    r = requests.post(FUND_URL, json=payload, timeout=30)
    return {"status_code": r.status_code, "body": r.json() if r.text else {}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default="")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--log", action="store_true")
    args = parser.parse_args()
    load_env()
    finalists = latest_finalists()
    wanted = {x.strip().upper() for x in args.tickers.split(",") if x.strip()}
    rows = [r for r in finalists if not wanted or first(r.get("ticker"), r.get("symbol")).upper() in wanted][:args.limit]
    out = {"model_key_status": {k: bool(os.environ.get(k)) for k in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"]}, "results": []}
    for row in rows:
        vals = values_for(row)
        user = render(USER_TEMPLATE, vals)
        raw = {
            "claude": call_claude(SYSTEM_PROMPT + "\n\n" + PERSONAS["claude"], user) if os.environ.get("ANTHROPIC_API_KEY") else None,
            "gpt": call_gpt(SYSTEM_PROMPT + "\n\n" + PERSONAS["gpt"], user) if os.environ.get("OPENAI_API_KEY") else None,
            "gemini": call_gemini(SYSTEM_PROMPT + "\n\n" + PERSONAS["gemini"], user) if os.environ.get("GEMINI_API_KEY") else None,
        }
        claude_parsed = (
            parse_model_json(raw["claude"], vals["ticker"])
            if raw["claude"] is not None
            else {
                "ticker": vals["ticker"], "verdict": "CLEAR", "confidence": 50,
                "upgrade_signals": [], "red_flags": ["claude_unavailable"],
                "reason": "Claude API key not configured", "force_skip": False,
                "size_view": "full",
            }
        )
        parsed = [
            {**claude_parsed, "_model": "claude"},
            {**parse_model_json(raw["gpt"], vals["ticker"]), "_model": "gpt"},
            {**parse_model_json(raw["gemini"], vals["ticker"]), "_model": "gemini"},
        ]
        verdict = merge_verdict(parsed)
        record = {"ticker": vals["ticker"], "raw_model_outputs": raw, "parsed_model_outputs": parsed, "merged_verdict": verdict}
        if args.log:
            record["idea_log_response"] = post_idea(row, verdict)
        out["results"].append(record)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
