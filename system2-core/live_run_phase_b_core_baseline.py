#!/usr/bin/env python3
"""
Nightly Phase B bare-core runner.

Order:
  B1 universe_builder.py
  catalyst and X discovery candidate feeds
  B2 cheap filter
  B3 Surgical Strike technical scoring
  Stage 3 news safety kill-filter
  Stage 4 options enrichment
  Stage 5 Chronos + Kronos forecast
  Stage 6 council (currently off)
  Stage 7 correlation/cluster guard
  log clustered finalists to /api/idea

Catalyst discovery is ride-along top-of-funnel only. Tagged candidates still
pass B2/B3/B4 like every scanner candidate and are measured via the scoring
loop if they survive.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

from regime_check import check_regime


ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path.home() / "Downloads"
RUN_LOG_DIR = ROOT / "logs"
RUN_LOG_DIR.mkdir(exist_ok=True)
REGIME_LATEST = ROOT / "regime_check_latest.json"
RUN_METADATA_URL = "http://127.0.0.1:3210/api/system2/run-metadata"


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_fmp_key() -> str:
    env_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if env_key:
        return env_key.strip()
    for path in [DOWNLOADS / "FMP-Scanner-v13.5-alpaca.json", DOWNLOADS / "FMP_Scanner_FIXED.json"]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"FMP_API_KEY:\s*'([^']+)'", text)
        if match:
            return match.group(1)
    raise RuntimeError("FMP API key not found. Set FMP_API_KEY.")


def run_step(name: str, args: list[str], env: dict[str, str]) -> dict:
    started = time.time()
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=1800,
    )
    return {
        "name": name,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "runtimeSeconds": round(time.time() - started, 2),
        "stdoutTail": proc.stdout[-4000:],
        "stderrTail": proc.stderr[-4000:],
    }


def post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "system2-regime-runner/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {"status": resp.status}


def send_telegram_alert(text: str) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return {"sent": False, "reason": "missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    return post_json(url, {"chat_id": chat_id, "text": text}, timeout=30)


def abort_metadata(run_date: str, regime: dict, run_started: str, telegram_result: dict) -> dict:
    return {
        "date": run_date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "regime_checked_at": regime.get("checked_at"),
        "regime": regime.get("regime"),
        "regime_reason": regime.get("reason"),
        "regime_aborted": True,
        "spy_1d_pct": regime.get("spy_1d_pct"),
        "qqq_1d_pct": regime.get("qqq_1d_pct"),
        "vix_current": regime.get("vix_current"),
        "vix_1d_chg": regime.get("vix_1d_chg"),
        "stages": [{
            "stage": "Regime",
            "in": 0,
            "out": 0,
            "dropped": 0,
            "reason": f"ABORTED - {regime.get('regime')}: {regime.get('reason')}",
            "mode": "SAFETY",
            "details": {"regime": regime, "telegram": telegram_result},
        }],
        "counts": {"universe": 0, "stage1": 0, "stage2": 0, "stage7": 0, "finalists": 0},
        "rejections": [],
        "near_miss_count": 0,
        "safety_filter_active": True,
        "safety_filter_removed_count": 0,
        "selection_logic_changed": False,
        "paper_only": True,
    }


def main() -> None:
    run_started = datetime.now(timezone.utc).isoformat()
    load_dotenv()
    env = os.environ.copy()
    env["FMP_API_KEY"] = load_fmp_key()
    env["PYTHONIOENCODING"] = "utf-8"
    run_date = datetime.now(timezone.utc).date().isoformat()

    regime = check_regime()
    regime["checked_at"] = datetime.now(timezone.utc).isoformat()
    regime["mode"] = "integrated_live_safety_check"
    REGIME_LATEST.write_text(json.dumps(regime, indent=2), encoding="utf-8")
    env["SYSTEM2_REGIME"] = regime.get("regime", "")
    env["SYSTEM2_REGIME_REASON"] = regime.get("reason", "")
    env["SYSTEM2_SPY_1D_PCT"] = str(regime.get("spy_1d_pct", ""))
    env["SYSTEM2_QQQ_1D_PCT"] = str(regime.get("qqq_1d_pct", ""))
    env["SYSTEM2_VIX_CURRENT"] = str(regime.get("vix_current", ""))
    env["SYSTEM2_VIX_1D_CHG"] = str(regime.get("vix_1d_chg", ""))

    print(json.dumps({"regime_check": regime}, indent=2))

    manual_force_run = os.environ.get("SYSTEM2_MANUAL_FORCE_RUN") == "1"
    if regime.get("regime") == "RISK_OFF" and not manual_force_run:
        msg = (
            "SYSTEM 2 - NO SCAN TONIGHT\n"
            f"Regime: RISK_OFF | {regime.get('reason')}\n"
            f"SPY {regime.get('spy_1d_pct')}% | QQQ {regime.get('qqq_1d_pct')}% | "
            f"VIX {regime.get('vix_current')}\n"
            "Watching for improvement. No positions tonight."
        )
        telegram_result = send_telegram_alert(msg)
        metadata = abort_metadata(run_date, regime, run_started, telegram_result)
        metadata_response = post_json(RUN_METADATA_URL, metadata)
        summary = {
            "runStartedAt": run_started,
            "runFinishedAt": datetime.now(timezone.utc).isoformat(),
            "ok": True,
            "aborted": True,
            "regime": regime,
            "telegram": telegram_result,
            "metadataResponse": metadata_response,
            "steps": [],
        }
        log_path = RUN_LOG_DIR / f"phase_b_core_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        log_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps({
            "ok": True,
            "aborted": True,
            "reason": regime.get("reason"),
            "log": str(log_path),
            "metadataResponse": metadata_response,
        }, indent=2))
        return

    steps = [
        ("B1 universe", ["universe_builder.py"]),
        ("Catalyst discovery", ["catalyst_discovery.py"]),
        ("X discovery", ["x_candidate_extractor.py"]),
        ("B2 cheap filter", ["b2_stage1_cheap_filter.py"]),
        ("B3 technical score", ["b3_surgical_strike_stage2.py"]),
        ("Stage3 news safety top40", ["c3_stage3_news_safety_filter.py", "--input", "stage2_surgical_strike_top40.json"]),
        ("Stage4 options ride-along", ["c1_options_flow_stage4_ridealong.py", "--input", "stage3_news_safe_top40.json", "--allow-no-data"]),
        ("Stage5 Chronos then Kronos", ["combined_forecast_stage5.py"]),
        ("Meta probability ride-along", ["meta_model_predict.py", "predict-file", "--input", "stage5_combined_forecast_top40.json", "--output", "stage5_combined_forecast_top40.json"]),
        ("Confluence ranking", ["confluence_scoring.py"]),
        ("B4 cluster guard", ["b4_correlation_cluster_engine.py"]),
        ("Log baseline ideas", ["log_phase_b_baseline_ideas.py", "--input", "stage7_clustered_survivors.json"]),
        ("Record run metadata", ["record_system2_run_metadata.py"]),
        ("Record stage details", ["record_system2_stage_details.py"]),
    ]

    results = []
    for name, args in steps:
        result = run_step(name, args, env)
        results.append(result)
        if not result["ok"]:
            break

    summary = {
        "runStartedAt": run_started,
        "runFinishedAt": datetime.now(timezone.utc).isoformat(),
        "ok": all(r["ok"] for r in results) and len(results) == len(steps),
        "steps": results,
    }

    for path_name, key in [
        ("universe.json", "universeCount"),
        ("catalyst_candidates.json", "catalystCandidateCount"),
        ("candidate_pool.json", "candidatePoolCount"),
        ("stage1_survivors.json", "stage1SurvivorCount"),
        ("stage2_surgical_strike_top40.json", "stage2TopCount"),
        ("stage5_combined_forecast_top40.json", "stage5ForecastCount"),
        ("stage3_news_safe_top40.json", "stage3NewsSafeCount"),
        ("stage4_options_enriched_top40.json", "stage4OptionsCount"),
        ("stage2_confluence_ranked_top40.json", "confluenceTopCount"),
        ("stage7_clustered_survivors.json", "stage7FinalistCount"),
    ]:
        path = ROOT / path_name
        if path.exists():
            summary[key] = len(json.loads(path.read_text(encoding="utf-8")))

    forecast_meta_path = ROOT / "stage5_combined_forecast_metadata.json"
    forecast_rows_path = ROOT / "stage5_combined_forecast_top40.json"
    forecast_meta = (
        json.loads(forecast_meta_path.read_text(encoding="utf-8"))
        if forecast_meta_path.exists()
        else {}
    )
    forecast_rows = (
        json.loads(forecast_rows_path.read_text(encoding="utf-8"))
        if forecast_rows_path.exists()
        else []
    )
    combined_fields_populated = sum(
        1
        for row in forecast_rows
        if row.get("combined_forecast_dir")
        and row.get("combined_band_pct") is not None
        and row.get("chronos_status")
        and row.get("kronos_status")
    )
    summary["chronos_inference_seconds"] = forecast_meta.get("chronos_inference_seconds")
    summary["kronos_inference_seconds"] = forecast_meta.get("kronos_inference_seconds")
    summary["total_forecast_seconds"] = forecast_meta.get("total_forecast_seconds")
    summary["combined_forecast_fields_populated"] = combined_fields_populated

    log_path = RUN_LOG_DIR / f"phase_b_core_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    log_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": summary["ok"],
        "log": str(log_path),
        "universeCount": summary.get("universeCount"),
        "stage1SurvivorCount": summary.get("stage1SurvivorCount"),
        "stage2TopCount": summary.get("stage2TopCount"),
        "stage7FinalistCount": summary.get("stage7FinalistCount"),
        "chronos_inference_seconds": summary.get("chronos_inference_seconds"),
        "kronos_inference_seconds": summary.get("kronos_inference_seconds"),
        "total_forecast_seconds": summary.get("total_forecast_seconds"),
        "combined_forecast_fields_populated": summary.get("combined_forecast_fields_populated"),
        "steps": [{"name": s["name"], "ok": s["ok"], "runtimeSeconds": s["runtimeSeconds"]} for s in results],
    }, indent=2))

    if not summary["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
