#!/usr/bin/env python3
"""
Nightly Phase B bare-core runner.

Order:
  B1 universe_builder.py
  catalyst discovery candidate feed
  B2 cheap filter
  B3 Surgical Strike technical scoring
  B4 correlation/cluster guard
  log clustered finalists to /api/idea

Catalyst discovery is ride-along top-of-funnel only. Tagged candidates still
pass B2/B3/B4 like every scanner candidate and are measured via the scoring
loop if they survive.

v2: Added run_id freshness stamps, pipeline failure handling, and stale-data
prevention. When any step fails, the run is marked FAILED, no stale counts
are reported, and a Telegram alert is sent.

v3: Fixed stamp_run_id for list-of-primitives files via companion .runstamp.json.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

from regime_check import check_regime
from stage_alignment_guard import check_stage_alignment


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


def stamp_run_id(path: Path, run_id: str) -> None:
    """Add _system2_run_id and _system2_generated_at to every JSON output file.

    For list files of dicts, stamps every object and writes a companion
    .runstamp.json so the health checker can read a top-level timestamp.
    For dict files, stamps top-level.
    For list files of primitives, writes a companion .runstamp.json file.
    Missing or unreadable files are silently skipped.
    """
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        ts = datetime.now(timezone.utc).isoformat()
        stamp_path = path.with_suffix(path.suffix + ".runstamp.json")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            for item in data:
                if isinstance(item, dict):
                    item["_system2_run_id"] = run_id
                    item["_system2_generated_at"] = ts
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            stamp_path.write_text(json.dumps({
                "_system2_run_id": run_id,
                "_system2_generated_at": ts,
                "_system2_source_file": path.name,
                "_system2_count": len(data),
            }, indent=2), encoding="utf-8")
        elif isinstance(data, dict):
            data["_system2_run_id"] = run_id
            data["_system2_generated_at"] = ts
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        else:
            # List of primitives or empty — write companion stamp file
            stamp_path.write_text(json.dumps({
                "_system2_run_id": run_id,
                "_system2_generated_at": ts,
                "_system2_source_file": path.name,
            }, indent=2), encoding="utf-8")
    except Exception:
        pass


def read_run_id_from_file(path: Path) -> str | None:
    """Read the _system2_run_id from a JSON file or its companion stamp."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0].get("_system2_run_id")
        elif isinstance(data, dict):
            return data.get("_system2_run_id")
        else:
            # List of primitives — check companion stamp
            stamp_path = path.with_suffix(path.suffix + ".runstamp.json")
            if stamp_path.exists():
                stamp = json.loads(stamp_path.read_text(encoding="utf-8", errors="ignore"))
                return stamp.get("_system2_run_id")
    except Exception:
        pass
    return None


def main() -> None:
    run_started = datetime.now(timezone.utc).isoformat()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    load_dotenv()
    env = os.environ.copy()
    env["FMP_API_KEY"] = load_fmp_key()
    env["PYTHONIOENCODING"] = "utf-8"
    env["SYSTEM2_RUN_ID"] = run_id
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

    print(json.dumps({"regime_check": regime, "run_id": run_id}, indent=2))

    if regime.get("regime") == "RISK_OFF":
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
            "runtimeMinutes": round((datetime.now(timezone.utc) - datetime.fromisoformat(run_started)).total_seconds() / 60, 2),
            "run_id": run_id,
            "pipeline_status": "ABORTED",
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

    # Map step names to their primary output files for run_id stamping.
    step_outputs: dict[str, list[str]] = {
        "B1 universe": ["universe.json"],
        "Catalyst discovery": ["catalyst_candidates.json"],
        "X discovery merge": ["candidate_pool.json"],
        "B2 cheap filter": ["stage1_survivors.json"],
        "B3 technical score": ["stage2_surgical_strike_top40.json", "stage2_surgical_strike_scored.json", "stage2_surgical_strike_metadata.json"],
        "FMP news + analyst signals": ["fmp_news_analyst_metadata.json"],
        "Signal2 seasonality": ["stage2_surgical_strike_top40.json"],
        "Signal3 dark pool proxy": ["stage2_surgical_strike_top40.json"],
        "Stage3 news safety top40": ["stage3_news_safe_top40.json"],
        "ImpliedOptions summaries": ["implied_options_scraper.log"],
        "Stage4 options ride-along": ["stage3_options_enriched_top40.json", "stage4_chronos_enriched_top40.json"],
        "Stage5 Chronos then Kronos": ["stage5_combined_forecast_top40.json"],
        "Meta probability ride-along": ["stage5_combined_forecast_top40.json"],
        "Social sentiment enrich": ["data/social_sentiment.json"],
        "Confluence ranking": ["stage2_confluence_ranked_top40.json", "stage2_confluence_metadata.json"],
        "Stage6 council v2": ["stage6_council_enriched.json", "council_stage6_metadata.json"],
        "B4 cluster guard": ["stage7_clustered_survivors.json"],
    }

    steps = [
        ("X discovery extract", ["x_candidate_extractor.py", "--extract-only"]),
        ("B1 universe", ["universe_builder.py"]),
        ("Catalyst discovery", ["catalyst_discovery.py"]),
        ("X discovery merge", ["x_candidate_extractor.py", "--merge-only"]),
        ("B2 cheap filter", ["b2_stage1_cheap_filter.py"]),
        ("B3 technical score", ["b3_surgical_strike_stage2.py"]),
        ("FMP news + analyst signals", ["fmp_news_analyst_signals.py"]),
        ("Signal2 seasonality", ["signal2_seasonality.py", "--input", "stage2_surgical_strike_top40.json", "--output", "stage2_surgical_strike_top40.json"]),
        ("Signal3 dark pool proxy", ["signal3_dark_pool_proxy.py", "--input", "stage2_surgical_strike_top40.json", "--output", "stage2_surgical_strike_top40.json"]),
        ("Stage3 news safety top40", ["c3_stage3_news_safety_filter.py", "--input", "stage2_surgical_strike_top40.json"]),
        ("ImpliedOptions summaries", ["implied_options_scraper.py", "--summaries-only", "--summary-input", "stage3_news_safe_top40.json"]),
        ("Stage4 options ride-along", ["c1_options_flow_stage4_ridealong.py", "--input", "stage3_news_safe_top40.json", "--allow-no-data"]),
        ("Stage5 Chronos then Kronos", ["combined_forecast_stage5.py"]),
        ("Meta probability ride-along", ["meta_model_predict.py", "predict-file", "--input", "stage5_combined_forecast_top40.json", "--output", "stage5_combined_forecast_top40.json"]),
        ("Social sentiment enrich", ["social_scraper.py", "--enrich", "stage5_combined_forecast_top40.json"]),
        ("Confluence ranking", ["confluence_scoring.py"]),
        ("Stage6 council v2", ["council_stage6_v2.py"]),
        ("B4 cluster guard", ["b4_correlation_cluster_engine.py"]),
        ("Log baseline ideas", ["log_phase_b_baseline_ideas.py", "--input", "stage7_clustered_survivors.json"]),
        ("Record run metadata", ["record_system2_run_metadata.py"]),
        ("Record stage details", ["record_system2_stage_details.py"]),
        ("Shadow portfolio", ["shadow_portfolio.py"]),
    ]

    results = []
    failed_step: dict | None = None
    for name, args in steps:
        # ═══════════════════════════════════════════════════════════════════
        # STAGE ALIGNMENT GUARD — before confluence scoring
        # ═══════════════════════════════════════════════════════════════════
        if name == "Confluence ranking":
            alignment = check_stage_alignment(min_overlap_pct=90.0)
            print(json.dumps({
                "stage": "alignment_guard",
                "ok": alignment.ok,
                "message": alignment.message,
                "overlap": {
                    "s4_pct": alignment.stage4_overlap_pct,
                    "s5_pct": alignment.stage5_overlap_pct,
                    "s6_pct": alignment.stage6_overlap_pct,
                },
            }, indent=2))
            if not alignment.ok:
                # Alignment failure — abort pipeline safely
                telegram_msg = (
                    f"⚠️ PIPELINE ALIGNMENT FAILURE {run_date}\n"
                    f"{alignment.message}\n"
                    f"Overlap: S4 {alignment.stage4_overlap_pct}% "
                    f"S5 {alignment.stage5_overlap_pct}% "
                    f"S6 {alignment.stage6_overlap_pct}%\n"
                    f"NO finalists posted. Enrichment is stale or ran on wrong symbol set. Investigate."
                )
                telegram_result = send_telegram_alert(telegram_msg)
                metadata = {
                    "date": run_date,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "regime_checked_at": regime.get("checked_at"),
                    "regime": regime.get("regime"),
                    "regime_reason": regime.get("reason"),
                    "alignment_failure": True,
                    "alignment_message": alignment.message,
                    "stage2_count": alignment.stage2_count,
                    "stage4_count": alignment.stage4_count,
                    "stage5_count": alignment.stage5_count,
                    "stage6_count": alignment.stage6_count,
                    "s4_overlap_pct": alignment.stage4_overlap_pct,
                    "s5_overlap_pct": alignment.stage5_overlap_pct,
                    "s6_overlap_pct": alignment.stage6_overlap_pct,
                    "spy_1d_pct": regime.get("spy_1d_pct"),
                    "qqq_1d_pct": regime.get("qqq_1d_pct"),
                    "vix_current": regime.get("vix_current"),
                    "vix_1d_chg": regime.get("vix_1d_chg"),
                    "stages": [{
                        "stage": "Alignment Guard",
                        "in": alignment.stage2_count,
                        "out": 0,
                        "dropped": alignment.stage2_count,
                        "reason": alignment.message,
                        "mode": "SAFETY",
                        "details": {"telegram": telegram_result, "alignment": alignment.__dict__},
                    }],
                    "counts": {"universe": 0, "stage1": 0, "stage2": alignment.stage2_count, "stage7": 0, "finalists": 0},
                    "rejections": [],
                    "near_miss_count": 0,
                    "safety_filter_active": True,
                    "safety_filter_removed_count": 0,
                    "selection_logic_changed": False,
                    "paper_only": True,
                }
                metadata_response = post_json(RUN_METADATA_URL, metadata)
                summary = {
                    "runStartedAt": run_started,
                    "runFinishedAt": datetime.now(timezone.utc).isoformat(),
                    "runtimeMinutes": round((datetime.now(timezone.utc) - datetime.fromisoformat(run_started)).total_seconds() / 60, 2),
                    "run_id": run_id,
                    "pipeline_status": "ALIGNMENT_FAILURE",
                    "ok": False,
                    "aborted": False,
                    "alignment_failure": True,
                    "regime": regime,
                    "telegram": telegram_result,
                    "metadataResponse": metadata_response,
                    "steps": [{"name": s["name"], "ok": s["ok"], "runtimeSeconds": s["runtimeSeconds"]} for s in results],
                }
                log_path = RUN_LOG_DIR / f"phase_b_core_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
                log_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
                print(json.dumps({
                    "ok": False,
                    "alignment_failure": True,
                    "reason": alignment.message,
                    "log": str(log_path),
                    "metadataResponse": metadata_response,
                }, indent=2))
                return

        result = run_step(name, args, env)
        results.append(result)

        # Non-fatal: FMP news + analyst signals 401
        if not result["ok"] and name == "FMP news + analyst signals":
            stderr = result.get("stderrTail", "")
            if "401" in stderr or "Unauthorized" in stderr:
                print(f"[WARNING] FMP news/analyst: HTTP 401 — plan does not include this endpoint. Continuing pipeline. Signal will be UNKNOWN/NEUTRAL for this run.")
                result["ok"] = True
                result["warning"] = "fmp_401_non_fatal"

        if result["ok"]:
            for out_name in step_outputs.get(name, []):
                stamp_run_id(ROOT / out_name, run_id)
        else:
            failed_step = result
            break

    # Build summary
    all_ok = all(r["ok"] for r in results) and len(results) == len(steps)
    pipeline_status = "SUCCESS" if all_ok else "FAILED"

    summary = {
        "runStartedAt": run_started,
        "runFinishedAt": datetime.now(timezone.utc).isoformat(),
        "runtimeMinutes": round((datetime.now(timezone.utc) - datetime.fromisoformat(run_started)).total_seconds() / 60, 2),
        "run_id": run_id,
        "pipeline_status": pipeline_status,
        "ok": all_ok,
        "steps": results,
    }

    if failed_step:
        summary["failed_at_step"] = failed_step["name"]
        summary["failure_reason"] = failed_step.get("stderrTail", "")[:500]

    # Only read counts from files that have matching run_id.
    # If the pipeline failed, we deliberately do NOT report stale counts.
    file_keys = [
        ("universe.json", "universeCount"),
        ("catalyst_candidates.json", "catalystCandidateCount"),
        ("candidate_pool.json", "candidatePoolCount"),
        ("stage1_survivors.json", "stage1SurvivorCount"),
        ("stage2_surgical_strike_top40.json", "stage2TopCount"),
        ("stage4_chronos_enriched_top40.json", "stage4ChronosCount"),
        ("stage3_options_enriched_top40.json", "stage3OptionsCount"),
        ("stage2_confluence_ranked_top40.json", "confluenceTopCount"),
        ("stage6_council_enriched.json", "councilCount"),
        ("stage7_clustered_survivors.json", "stage7FinalistCount"),
        ("stage5_news_safe_finalists.json", "stage5NewsSafeCount"),
        ("fmp_news_analyst_metadata.json", "newsAnalystCount"),
    ]

    for path_name, key in file_keys:
        path = ROOT / path_name
        if not path.exists():
            continue
        file_run_id = read_run_id_from_file(path)
        if file_run_id == run_id:
            try:
                summary[key] = len(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                summary[key] = None
        else:
            # Stale file from a previous run — do NOT report its count.
            summary[key + "_stale"] = True
            summary[key] = None

    summary["duration_minutes"] = summary.get("runtimeMinutes")
    summary["finalist_count"] = summary.get("stage7FinalistCount") or summary.get("stage5NewsSafeCount")
    summary["funnel_counts"] = {
        "universe": summary.get("universeCount"),
        "stage1": summary.get("stage1SurvivorCount"),
        "stage2": summary.get("stage2TopCount"),
        "finalists": summary.get("finalist_count"),
    }

    log_path = RUN_LOG_DIR / f"phase_b_core_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    log_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Send Telegram on failure
    if not all_ok:
        fail_name = failed_step["name"] if failed_step else "unknown"
        fail_reason = (failed_step.get("stderrTail", "")[:300] if failed_step else "")
        telegram_msg = (
            f"🛑 PIPELINE FAILED {run_date}\n"
            f"Stopped at: {fail_name}\n"
            f"Error: {fail_reason}\n"
            f"NO finalists generated. Previous run's data is NOT being reused.\n"
            f"Fix required before next run."
        )
        telegram_result = send_telegram_alert(telegram_msg)
        summary["telegram_alert"] = telegram_result
        # Rewrite log with telegram result
        log_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps({
        "ok": summary["ok"],
        "pipeline_status": pipeline_status,
        "run_id": run_id,
        "log": str(log_path),
        "universeCount": summary.get("universeCount"),
        "stage1SurvivorCount": summary.get("stage1SurvivorCount"),
        "stage2TopCount": summary.get("stage2TopCount"),
        "stage7FinalistCount": summary.get("stage7FinalistCount"),
        "steps": [{"name": s["name"], "ok": s["ok"], "runtimeSeconds": s["runtimeSeconds"]} for s in results],
    }, indent=2))

    if not summary["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
