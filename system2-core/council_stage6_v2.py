#!/usr/bin/env python3
'''Council of AIs v2 — 4-model 2-round hybrid deliberation for System 2 Stage 6.

Paper mode only. council_gates_trades = false.
'''

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
INPUT_PATH = ROOT / "stage2_confluence_ranked_top40.json"
OUTPUT_PATH = ROOT / "stage6_council_enriched.json"
META_PATH = ROOT / "council_stage6_metadata.json"


def _delete_stale_outputs(output: Path, metadata: Path) -> None:
    """Remove previous council outputs so a failed/timeout run cannot leave stale data."""
    for path in (output, metadata):
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


COUNCIL_GATES_TRADES = False
TIMEOUT_SECONDS = 15
GEMINI_TIMEOUT_SECONDS = 25
MAX_RETRIES = 0
RETRY_DELAY = 1

ENABLE_ROUND2 = False
TOTAL_TIMEOUT_SECONDS = 480
PER_IDEA_TIMEOUT_SECONDS = 45

VERDICTS = {"UPGRADE", "TIER1", "TIER2", "TIER3", "SKIP", "FORCE_SKIP"}
CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def num(value, default=None) -> float | None:
    try:
        if value in (None, ""):
            return default
        v = float(value)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def text(value) -> str:
    return str(value or "").strip()


def extract_json_block(raw: str) -> dict | None:
    '''Try to extract a JSON object from model response text.'''
    raw = raw.strip()
    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try markdown code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try first { … } block
    m = re.search(r"(\{.*?\})", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def sanitize_verdict(v: Any) -> str:
    v = text(v).upper()
    return v if v in VERDICTS else "SKIP"


def sanitize_confidence(v: Any) -> str:
    v = text(v).upper()
    return v if v in CONFIDENCES else "LOW"


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL CLIENTS
# ═══════════════════════════════════════════════════════════════════════════════

def _post_json(url: str, payload: dict, headers: dict[str, str], timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "system2-council-v2/1.0",
        **headers,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def call_claude(prompt: str, api_key: str) -> dict[str, Any]:
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = _post_json(
        "https://api.anthropic.com/v1/messages",
        payload,
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        TIMEOUT_SECONDS,
    )
    content = resp.get("content", [])
    raw = content[0].get("text", "") if content else ""
    parsed = extract_json_block(raw) or {}
    return {
        "verdict": sanitize_verdict(parsed.get("verdict")),
        "confidence": sanitize_confidence(parsed.get("confidence")),
        "reasoning": text(parsed.get("reasoning")) or raw[:300],
        "raw": raw[:800],
    }


def call_kimi(prompt: str, api_key: str) -> dict[str, Any]:
    payload = {
        "model": "moonshot-v1-8k",
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = _post_json(
        "https://api.moonshot.cn/v1/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
        TIMEOUT_SECONDS,
    )
    choice = (resp.get("choices") or [{}])[0]
    raw = text(choice.get("message", {}).get("content"))
    parsed = extract_json_block(raw) or {}
    return {
        "verdict": sanitize_verdict(parsed.get("verdict")),
        "confidence": sanitize_confidence(parsed.get("confidence")),
        "reasoning": text(parsed.get("reasoning")) or raw[:300],
        "raw": raw[:800],
    }


def call_gemini(prompt: str, api_key: str) -> dict[str, Any]:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    resp = _post_json(url, payload, {}, GEMINI_TIMEOUT_SECONDS)
    candidates = resp.get("candidates", [])
    raw = ""
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            raw = text(parts[0].get("text"))
    parsed = extract_json_block(raw) or {}
    return {
        "verdict": sanitize_verdict(parsed.get("verdict")),
        "confidence": sanitize_confidence(parsed.get("confidence")),
        "reasoning": text(parsed.get("reasoning")) or raw[:300],
        "raw": raw[:800],
    }


def call_gpt4o(prompt: str, api_key: str) -> dict[str, Any]:
    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = _post_json(
        "https://api.openai.com/v1/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
        TIMEOUT_SECONDS,
    )
    choice = (resp.get("choices") or [{}])[0]
    raw = text(choice.get("message", {}).get("content"))
    parsed = extract_json_block(raw) or {}
    return {
        "verdict": sanitize_verdict(parsed.get("verdict")),
        "confidence": sanitize_confidence(parsed.get("confidence")),
        "reasoning": text(parsed.get("reasoning")) or raw[:300],
        "raw": raw[:800],
    }


MODEL_CONFIG: dict[str, dict[str, Any]] = {
    "claude": {
        "name": "Claude",
        "role": "The Analyst",
        "key_env": "ANTHROPIC_API_KEY",
        "caller": call_claude,
    },
    "kimi": {
        "name": "Kimi",
        "role": "The Devil's Advocate",
        "key_env": "KIMI_API_KEY",
        "caller": call_kimi,
    },
    "gemini": {
        "name": "Gemini",
        "role": "The Short Seller",
        "key_env": "GEMINI_API_KEY",
        "caller": call_gemini,
    },
    "gpt4o": {
        "name": "GPT-4o",
        "role": "The Judge",
        "key_env": "OPENAI_API_KEY",
        "caller": call_gpt4o,
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════════════════════════════════════════

def idea_payload_text(idea: dict) -> str:
    return json.dumps({
        "symbol": idea.get("symbol"),
        "setup_score": idea.get("setupQualityScore") or idea.get("setup_score"),
        "confluence_score": idea.get("confluence_score"),
        "grade": idea.get("grade"),
        "sector": idea.get("sector"),
        "entry_zone": idea.get("entryZone") or idea.get("entry"),
        "stop": idea.get("stopLoss") or idea.get("stop"),
        "tp1": idea.get("tp1"),
        "tp2": idea.get("tp2"),
        "rr": idea.get("riskReward") or idea.get("rr"),
        "vix_regime": idea.get("regime") or os.environ.get("SYSTEM2_REGIME"),
        "track_a_pass": not idea.get("track_a_fail"),
        "track_a_fail": idea.get("track_a_fail"),
        "signals": idea.get("scoreReasons") or [],
        "options_verdict": idea.get("options_verdict"),
        "options_score": idea.get("options_signals_count"),
        "iv_rank": idea.get("iv_rank_proxy") or idea.get("iv_rank"),
        "seasonal_signal": idea.get("seasonal_signal"),
        "dark_pool_signal": idea.get("dark_pool_signal"),
        "chronos_forecast": idea.get("chronos_dir"),
        "kronos_forecast": idea.get("kronos_dir"),
        "spy_rs": idea.get("rsVsSpy"),
        "sector_alpha": idea.get("sectorAlpha"),
    }, indent=2)


PERSONA_PROMPTS = {
    "claude": (
        "You are The Analyst (Claude). Your job is to evaluate a swing-trade setup honestly and specifically.\n\n"
        "You are given a JSON payload with the trade idea. Score the setup. What is the bull case? What are the real risks? Be specific to the data provided, not generic.\n\n"
        'Respond ONLY with a JSON object in this exact format:\n'
        '{"verdict": "UPGRADE" or "TIER1" or "TIER2" or "TIER3" or "SKIP" or "FORCE_SKIP", "confidence": "HIGH" or "MEDIUM" or "LOW", "reasoning": "max 3 sentences, specific to this idea"}\n\n'
        "Idea payload:\n{payload}"
    ),
    "kimi": (
        "You are The Devil's Advocate (Kimi). Your job is to challenge assumptions.\n\n"
        "You are given a JSON payload with a swing-trade setup. Question US-centric bias. Look for macro and global factors the analyst missed. You MUST find a specific reason to disagree or explicitly state you cannot.\n\n"
        'Respond ONLY with a JSON object in this exact format:\n'
        '{"verdict": "UPGRADE" or "TIER1" or "TIER2" or "TIER3" or "SKIP" or "FORCE_SKIP", "confidence": "HIGH" or "MEDIUM" or "LOW", "reasoning": "max 3 sentences, specific to this idea"}\n\n'
        "Idea payload:\n{payload}"
    ),
    "gemini": (
        "You are The Short Seller (Gemini). You are cynical and bearish by default.\n\n"
        "You are given a JSON payload with a swing-trade setup. Make the bear case. Would you short this? Rate the downside risk. Be specific to the data.\n\n"
        'Respond ONLY with a JSON object in this exact format:\n'
        '{"verdict": "UPGRADE" or "TIER1" or "TIER2" or "TIER3" or "SKIP" or "FORCE_SKIP", "confidence": "HIGH" or "MEDIUM" or "LOW", "reasoning": "max 3 sentences, specific to this idea"}\n\n'
        "Idea payload:\n{payload}"
    ),
    "gpt4o": (
        "You are The Judge (GPT-4o). You are a senior portfolio manager.\n\n"
        "You are given a JSON payload with a swing-trade setup. Weigh the setup quality, risk, and edge. Make a balanced ruling. No personal bias — purely adjudicate the idea on its merits.\n\n"
        'Respond ONLY with a JSON object in this exact format:\n'
        '{"verdict": "UPGRADE" or "TIER1" or "TIER2" or "TIER3" or "SKIP" or "FORCE_SKIP", "confidence": "HIGH" or "MEDIUM" or "LOW", "reasoning": "max 3 sentences, specific to this idea"}\n\n'
        "Idea payload:\n{payload}"
    ),
}


def round2_kimi_prompt(idea: dict, round1: dict[str, dict]) -> str:
    payload = idea_payload_text(idea)
    summaries = []
    for model, result in round1.items():
        cfg = MODEL_CONFIG[model]
        summaries.append(
            f"- {cfg['name']} ({cfg['role']}): {result['verdict']} / {result['confidence']}\n  Reasoning: {result['reasoning']}"
        )
    return (
        "The council is split on this trade. Here are the Round 1 verdicts:\n\n"
        f"{chr(10).join(summaries)}\n\n"
        "You are Kimi, The Devil's Advocate. Review all arguments above. Has anything changed your view? Identify the single strongest argument FOR and AGAINST this trade.\n\n"
        'Then respond ONLY with a JSON object:\n'
        '{"verdict": "UPGRADE" or "TIER1" or "TIER2" or "TIER3" or "SKIP" or "FORCE_SKIP", "confidence": "HIGH" or "MEDIUM" or "LOW", "reasoning": "max 3 sentences"}\n\n'
        f"Idea payload:\n{payload}"
    )


def round2_gemini_prompt(idea: dict, round1: dict[str, dict], kimi_r2: dict) -> str:
    payload = idea_payload_text(idea)
    summaries = []
    for model, result in round1.items():
        cfg = MODEL_CONFIG[model]
        summaries.append(
            f"- {cfg['name']} ({cfg['role']}): {result['verdict']} / {result['confidence']}\n  Reasoning: {result['reasoning']}"
        )
    return (
        "The council is split. Here are the Round 1 verdicts:\n\n"
        f"{chr(10).join(summaries)}\n\n"
        f"The Devil's Advocate (Kimi) has weighed in Round 2:\n"
        f"- Verdict: {kimi_r2['verdict']} / {kimi_r2['confidence']}\n"
        f"- Reasoning: {kimi_r2['reasoning']}\n\n"
        "You are Gemini, The Short Seller. Does the bear case still hold? What is the one thing that would make you wrong?\n\n"
        'Then respond ONLY with a JSON object:\n'
        '{"verdict": "UPGRADE" or "TIER1" or "TIER2" or "TIER3" or "SKIP" or "FORCE_SKIP", "confidence": "HIGH" or "MEDIUM" or "LOW", "reasoning": "max 3 sentences"}\n\n'
        f"Idea payload:\n{payload}"
    )


def round2_gpt4o_prompt(idea: dict, round1: dict[str, dict], kimi_r2: dict, gemini_r2: dict) -> str:
    payload = idea_payload_text(idea)
    summaries = []
    for model, result in round1.items():
        cfg = MODEL_CONFIG[model]
        summaries.append(
            f"- {cfg['name']} ({cfg['role']}): {result['verdict']} / {result['confidence']}\n  Reasoning: {result['reasoning']}"
        )
    return (
        "You are GPT-4o, The Judge — senior portfolio manager. The council is split. You have heard all arguments.\n\n"
        "Round 1 verdicts:\n"
        f"{chr(10).join(summaries)}\n\n"
        "Round 2 deliberation:\n"
        f"- Devil's Advocate (Kimi): {kimi_r2['verdict']} / {kimi_r2['confidence']}\n"
        f'  "{kimi_r2["reasoning"]}"\n'
        f"- Short Seller (Gemini): {gemini_r2['verdict']} / {gemini_r2['confidence']}\n"
        f'  "{gemini_r2["reasoning"]}"\n\n'
        "Make the final ruling. Choose one verdict and give one sentence of reasoning. Your ruling is final.\n\n"
        'Respond ONLY with a JSON object:\n'
        '{"verdict": "UPGRADE" or "TIER1" or "TIER2" or "TIER3" or "SKIP" or "FORCE_SKIP", "confidence": "HIGH" or "MEDIUM" or "LOW", "reasoning": "one sentence"}\n\n'
        f"Idea payload:\n{payload}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════════

def call_model_safe(model_key: str, prompt: str) -> dict[str, Any]:
    cfg = MODEL_CONFIG[model_key]
    api_key = os.environ.get(cfg["key_env"], "").strip()
    if not api_key:
        return {
            "verdict": "ABSTAIN",
            "confidence": "LOW",
            "reasoning": f"{cfg['name']} — API key missing",
            "raw": "",
            "error": "API key missing",
        }
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = cfg["caller"](prompt, api_key)
            result["error"] = None
            return result
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            return {
                "verdict": "ERROR",
                "confidence": "LOW",
                "reasoning": f"{cfg['name']} call failed: {error_msg}",
                "raw": "",
                "error": error_msg,
            }
    return {
        "verdict": "ERROR",
        "confidence": "LOW",
        "reasoning": f"{cfg['name']} unknown error",
        "raw": "",
        "error": "unknown",
    }


def round1_for_idea(idea: dict, idea_timeout: int = PER_IDEA_TIMEOUT_SECONDS) -> dict[str, Any]:
    payload = idea_payload_text(idea)
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(call_model_safe, mk, PERSONA_PROMPTS[mk].replace("{payload}", payload)): mk
            for mk in MODEL_CONFIG
        }
        done, not_done = wait(futures.keys(), timeout=idea_timeout)
        for future in not_done:
            future.cancel()
            mk = futures[future]
            cfg = MODEL_CONFIG[mk]
            results[mk] = {
                "verdict": "TIMEOUT",
                "confidence": "LOW",
                "reasoning": f"{cfg['name']} timed out after {idea_timeout}s",
                "raw": "",
                "error": f"timeout after {idea_timeout}s",
            }
        for future in done:
            mk = futures[future]
            try:
                results[mk] = future.result(timeout=1)
            except Exception as exc:
                cfg = MODEL_CONFIG[mk]
                results[mk] = {
                    "verdict": "ERROR",
                    "confidence": "LOW",
                    "reasoning": f"{cfg['name']} thread error: {exc}",
                    "raw": "",
                    "error": str(exc),
                }
    return results


def should_trigger_round2(round1: dict[str, dict]) -> bool:
    active = {k: v for k, v in round1.items() if v["verdict"] not in {"ABSTAIN", "ERROR"}}
    if not active:
        return False
    verdicts = [v["verdict"] for v in active.values()]
    # Any FORCE_SKIP triggers Round 2
    if "FORCE_SKIP" in verdicts:
        return True
    # Check for 2v2 split
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v] = counts.get(v, 0) + 1
    max_count = max(counts.values())
    # If no majority (e.g., 2 vs 2 with 4 active, or 1-1-1 with 3 active)
    if max_count < len(verdicts) / 2:
        return True
    return False


def round2_for_idea(idea: dict, round1: dict[str, dict]) -> dict[str, Any]:
    # Step 1: Kimi Round 2
    kimi_r2 = call_model_safe("kimi", round2_kimi_prompt(idea, round1))
    # Step 2: Gemini Round 2
    gemini_r2 = call_model_safe("gemini", round2_gemini_prompt(idea, round1, kimi_r2))
    # Step 3: GPT-4o final ruling
    gpt4o_r2 = call_model_safe("gpt4o", round2_gpt4o_prompt(idea, round1, kimi_r2, gemini_r2))
    return {
        "kimi_r2": kimi_r2,
        "gemini_r2": gemini_r2,
        "gpt4o_r2": gpt4o_r2,
        "final_verdict": gpt4o_r2["verdict"],
        "final_confidence": gpt4o_r2["confidence"],
        "final_reasoning": gpt4o_r2["reasoning"],
    }


def compute_quorum(round1: dict[str, dict]) -> str:
    active = [k for k, v in round1.items() if v["verdict"] not in {"ABSTAIN", "ERROR"}]
    if len(active) >= 4:
        return "FULL"
    if len(active) == 3:
        return "FULL"
    if len(active) == 2:
        return "REDUCED"
    if len(active) == 1:
        return "SINGLE_MODEL"
    return "NONE"


def finalize_idea(idea: dict, round1: dict[str, dict], round2: dict[str, Any] | None) -> dict[str, Any]:
    active = {k: v for k, v in round1.items() if v["verdict"] not in {"ABSTAIN", "ERROR"}}
    abstained = [k for k, v in round1.items() if v["verdict"] in {"ABSTAIN", "ERROR"}]

    if not active:
        final_verdict = "SKIP"
        final_confidence = "LOW"
        final_reasoning = "All models abstained or errored"
        council_round = 0
        unanimous = False
        split = False
    elif round2:
        final_verdict = round2["final_verdict"]
        final_confidence = round2["final_confidence"]
        final_reasoning = round2["final_reasoning"]
        council_round = 2
        unanimous = False
        split = True
    else:
        # Round 1 majority
        counts: dict[str, int] = {}
        for v in active.values():
            counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
        final_verdict = max(counts, key=counts.get)
        # Use confidence of the first model with the majority verdict
        final_confidence = "LOW"
        final_reasoning = ""
        for mk, v in active.items():
            if v["verdict"] == final_verdict:
                final_confidence = v["confidence"]
                final_reasoning = v["reasoning"]
                break
        council_round = 1
        unanimous = len(counts) == 1
        split = not unanimous and len(active) >= 4 and max(counts.values()) == 2

    out = {
        **idea,
        "council_gates_trades": COUNCIL_GATES_TRADES,
        "council_final_verdict": final_verdict,
        "council_round": council_round,
        "council_quorum": compute_quorum(round1),
        "council_models_used": list(active.keys()),
        "council_models_abstained": abstained,
        "council_unanimous": unanimous,
        "council_split": split,
    }
    for mk in MODEL_CONFIG:
        r = round1.get(mk, {})
        out[f"{mk}_verdict"] = r.get("verdict")
        out[f"{mk}_confidence"] = r.get("confidence")
        out[f"{mk}_reasoning"] = r.get("reasoning")
        out[f"{mk}_error"] = r.get("error")
    if round2:
        out["kimi_r2_verdict"] = round2["kimi_r2"]["verdict"]
        out["kimi_r2_confidence"] = round2["kimi_r2"]["confidence"]
        out["kimi_r2_reasoning"] = round2["kimi_r2"]["reasoning"]
        out["gemini_r2_verdict"] = round2["gemini_r2"]["verdict"]
        out["gemini_r2_confidence"] = round2["gemini_r2"]["confidence"]
        out["gemini_r2_reasoning"] = round2["gemini_r2"]["reasoning"]
        out["gpt4o_r2_verdict"] = round2["gpt4o_r2"]["verdict"]
        out["gpt4o_r2_confidence"] = round2["gpt4o_r2"]["confidence"]
        out["gpt4o_r2_reasoning"] = round2["gpt4o_r2"]["reasoning"]
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def _write_skipped_outputs(rows, output_path: Path, meta_path: Path, reason: str) -> None:
    """Write a safe fallback council output when the stage cannot complete."""
    enriched = []
    metadata_records = []
    verdict_distribution: dict[str, int] = {}
    for idea in rows:
        symbol = text(idea.get("symbol"))
        result = {
            **idea,
            "council_gates_trades": COUNCIL_GATES_TRADES,
            "council_final_verdict": "SKIP",
            "council_round": 0,
            "council_quorum": "NONE",
            "council_models_used": [],
            "council_models_abstained": list(MODEL_CONFIG.keys()),
            "council_unanimous": False,
            "council_split": False,
            "council_status": "SKIPPED",
            "council_skip_reason": reason,
        }
        for mk in MODEL_CONFIG:
            result[f"{mk}_verdict"] = "ABSTAIN"
            result[f"{mk}_confidence"] = "LOW"
            result[f"{mk}_reasoning"] = reason
            result[f"{mk}_error"] = reason
        enriched.append(result)
        metadata_records.append({
            "symbol": symbol,
            "round": 0,
            "quorum": "NONE",
            "final_verdict": "SKIP",
            "models_used": [],
            "models_abstained": list(MODEL_CONFIG.keys()),
        })
        verdict_distribution["SKIP"] = verdict_distribution.get("SKIP", 0) + 1
    metadata = {
        "stage": "COUNCIL_V2",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "inputCount": len(rows),
        "outputCount": len(enriched),
        "round2Triggered": 0,
        "councilGatesTrades": COUNCIL_GATES_TRADES,
        "verdictDistribution": verdict_distribution,
        "totalTimeoutReached": False,
        "skipped": True,
        "skipReason": reason,
        "details": metadata_records,
        "paperOnly": True,
    }
    output_path.write_text(json.dumps(enriched, indent=2), encoding="utf-8")
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(INPUT_PATH))
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--metadata", default=str(META_PATH))
    parser.add_argument("--limit", type=int, default=0, help="Process only first N ideas (for testing)")
    args = parser.parse_args()

    output_path = Path(args.output)
    meta_path = Path(args.metadata)
    _delete_stale_outputs(output_path, meta_path)

    load_dotenv()
    try:
        rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Council failed to read input: {exc}")
        _write_skipped_outputs([], output_path, meta_path, f"input read failed: {exc}")
        return

    if args.limit > 0:
        rows = rows[:args.limit]

    try:
        enriched = []
        metadata_records = []
        round2_count = 0
        verdict_distribution: dict[str, int] = {}
        start_time = time.time()
        total_timeout_reached = False

        for idx, idea in enumerate(rows):
            elapsed = time.time() - start_time
            if elapsed > TOTAL_TIMEOUT_SECONDS:
                total_timeout_reached = True
                remaining_count = len(rows) - idx
                print(f"Council timeout after {int(elapsed)}s — {idx} of {len(rows)} ideas scored; {remaining_count} remaining marked TIMEOUT")
                for remaining in rows[idx:]:
                    symbol = text(remaining.get("symbol"))
                    result = {
                        **remaining,
                        "council_gates_trades": COUNCIL_GATES_TRADES,
                        "council_final_verdict": "SKIP",
                        "council_round": 0,
                        "council_quorum": "NONE",
                        "council_models_used": [],
                        "council_models_abstained": list(MODEL_CONFIG.keys()),
                        "council_unanimous": False,
                        "council_split": False,
                        "council_status": "TIMEOUT",
                    }
                    for mk in MODEL_CONFIG:
                        result[f"{mk}_verdict"] = "TIMEOUT"
                        result[f"{mk}_confidence"] = "LOW"
                        result[f"{mk}_reasoning"] = "Council stage timeout"
                        result[f"{mk}_error"] = "Council stage timeout"
                    enriched.append(result)
                    metadata_records.append({
                        "symbol": symbol,
                        "round": 0,
                        "quorum": "NONE",
                        "final_verdict": "SKIP",
                        "models_used": [],
                        "models_abstained": list(MODEL_CONFIG.keys()),
                    })
                    verdict_distribution["SKIP"] = verdict_distribution.get("SKIP", 0) + 1
                break

            symbol = text(idea.get("symbol"))
            print(f"Council Round 1: {symbol}")
            round1 = round1_for_idea(idea)
            round2 = None
            if ENABLE_ROUND2 and should_trigger_round2(round1):
                print(f"  → Round 2 triggered for {symbol}")
                round2 = round2_for_idea(idea, round1)
                round2_count += 1
            result = finalize_idea(idea, round1, round2)
            enriched.append(result)
            verdict_distribution[result["council_final_verdict"]] = verdict_distribution.get(result["council_final_verdict"], 0) + 1
            metadata_records.append({
                "symbol": symbol,
                "round": result["council_round"],
                "quorum": result["council_quorum"],
                "final_verdict": result["council_final_verdict"],
                "models_used": result["council_models_used"],
                "models_abstained": result["council_models_abstained"],
            })

        metadata = {
            "stage": "COUNCIL_V2",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "inputCount": len(rows),
            "outputCount": len(enriched),
            "round2Triggered": round2_count,
            "councilGatesTrades": COUNCIL_GATES_TRADES,
            "verdictDistribution": verdict_distribution,
            "totalTimeoutReached": total_timeout_reached,
            "details": metadata_records,
            "paperOnly": True,
        }

        output_path.write_text(json.dumps(enriched, indent=2), encoding="utf-8")
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(json.dumps(metadata, indent=2))
    except Exception as exc:
        print(f"Council stage failed: {exc}")
        _write_skipped_outputs(rows, output_path, meta_path, f"council stage failed: {exc}")


if __name__ == "__main__":
    main()
