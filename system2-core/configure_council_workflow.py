#!/usr/bin/env python3
"""Normalize the inactive Council workflow for System 2 ride-along testing."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PERSONAS = {
    "Claude Evaluator": (
        "You are the BALANCED REVIEWER. Look for both risks and genuine "
        "strengths with equal weight."
    ),
    "ChatGPT Evaluator": (
        "You are the MOMENTUM CHASER. Focus on whether trend, volume, and "
        "catalyst are strong enough to sustain a 2-10 day move. What would "
        "stop this momentum?"
    ),
    "Gemini Evaluator": (
        "You are the CYNICAL SHORT SELLER. Your job is to find every reason "
        "this trade fails. You are evaluated on whether you identify risks "
        "others missed, NOT on agreement. Look hard before saying CLEAR."
    ),
}

SYSTEM_PROMPT = """You are a SENIOR TRADE REVIEWER for a systematic US equity
swing trading system. Your job has two equally important parts:

PART 1 - RED FLAG DETECTION (primary job):
Identify anything that would make a trader regret taking this trade. Be
specific and honest. Do not manufacture concerns to seem cautious.

PART 2 - UPGRADE DETECTION (secondary job):
Identify genuine positive signals BEYOND what the technical scanner already
measured. An upgrade requires something the scanner cannot see -
institutional footprints, catalyst timing, multi-layer convergence. Do not
upgrade simply because technical signals look strong.

YOU ARE NOT A STOCK PICKER. You review specific trade setup data and flag
what is unusually good or bad for a 2-10 day swing hold.

OUTPUT: strict JSON only. No preamble, no markdown, no code blocks. Raw JSON.
Parsed by code. Deviations = errors.

DO NOT: hallucinate news, use stale data, refuse JSON, give vague generic
responses. Be specific or be silent."""

USER_PROMPT = """REVIEW THIS SWING TRADE SETUP. Return ONLY the JSON below.

DATE: {{date}}
TICKER: {{ticker}}
SECTOR: {{sector}}
SETUP_TYPE: {{setup_type}}
GRADE: {{grade}} ({{setup_score}}/100)
CONFLUENCE: {{confluence_score}}/130
TIME_HORIZON: Swing 2-10 days

TECHNICAL:
  RVOL: {{rvol}}x | RS vs SPY: {{rs_vs_spy}}%
  VWAP distance: {{vwap_pct}}% | ATR: {{atr_daily}}
  Sector RS: {{sector_rs}}%

TRADE PLAN:
  Entry: {{entry_low}}-{{entry_high}}
  Stop: {{stop}} ({{stop_atr_multiple}}x ATR)
  TP1: {{tp1}} / TP2: {{tp2}} | R:R: {{rr}}
  Size: {{shares}} shares / ${{risk_dollars}} risk

ENRICHMENT:
  Options: {{options_verdict}} ({{options_signals_count}}/4 signals, IV rank {{iv_rank}})
  Forecast: {{combined_forecast_dir}}
    (Chronos {{chronos_dir}} {{chronos_band_pct}}%,
     Kronos {{kronos_dir}} {{kronos_band_pct}}%)
  Catalyst: {{catalyst_summary}} ({{sub_type}})
  Analyst change: {{analyst_change}}

MARKET:
  Regime: {{regime}} | SPY: {{spy_1d_pct}}%
  QQQ: {{qqq_1d_pct}}% | VIX: {{vix_current}}

UPGRADE SIGNALS (exact strings only, or []):
  "dark_pool_accumulation" "options_sweep_bullish"
  "multi_layer_convergence" "catalyst_imminent"
  "sector_breakout_timing" "insider_cluster_buy"
  "chronos_tight_bullish"

RED FLAGS (exact strings only, or []):
  "earnings_risk" "sector_weakness" "overextended"
  "low_liquidity" "adverse_news" "dilution_risk"
  "low_options_conviction" "wide_chronos_cone"
  "regime_mismatch" "correlation_warning"
  "weak_catalyst" "gap_risk" "fundamental_concern"
  "momentum_fading"

FORCE_SKIP only for: earnings <48h confirmed, confirmed halt/fraud/dilution,
VIX>35 AND long setup, gap >2x ATR against setup.

RETURN EXACTLY THIS JSON - nothing else:
{
  "ticker": "{{ticker}}",
  "verdict": "STRONG"|"CLEAR"|"WEAK"|"SKIP"|"FORCE_SKIP",
  "confidence": <0-100>,
  "upgrade_signals": [],
  "red_flags": [],
  "reason": "<max 25 words specific to this setup>",
  "force_skip": true|false,
  "size_view": "increase"|"full"|"reduce"|"half"|"zero"
}

STRONG requires naming at least one upgrade signal.
You are one of three independent reviewers."""


def one_workflow(payload):
    return payload[0] if isinstance(payload, list) else payload


def node(workflow, name):
    return next(item for item in workflow["nodes"] if item["name"] == name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    workflow = one_workflow(json.loads(Path(args.input).read_text(encoding="utf-8")))
    workflow["active"] = False
    workflow["name"] = "Council of AIs - System 2 Ride-Along"

    schedule = node(workflow, "Schedule (Market Hours ET)")
    schedule["name"] = "Schedule 03:30 UTC Tue-Sat (INACTIVE)"
    schedule["parameters"] = {
        "rule": {
            "interval": [{
                "field": "cronExpression",
                "expression": "30 3 * * 2-6",
            }]
        }
    }

    prepare = node(workflow, "Prepare Council Payloads")
    prepare_code = prepare["parameters"]["jsCode"]
    prepare_code = re.sub(
        r'const SYSTEM_PROMPT = .*?;\nconst USER_TEMPLATE = .*?;\n',
        "const SYSTEM_PROMPT = " + json.dumps(SYSTEM_PROMPT) + ";\n"
        "const USER_TEMPLATE = " + json.dumps(USER_PROMPT) + ";\n",
        prepare_code,
        count=1,
        flags=re.S,
    )
    prepare_code = prepare_code.replace(
        "chronos_band_pct: first(row.chronos_band_pct),",
        """chronos_band_pct: first(row.chronos_band_pct),
    kronos_dir: first(row.kronos_dir, row.kronos_direction),
    kronos_band_pct: first(row.kronos_band_pct),
    combined_forecast_dir: first(row.combined_forecast_dir, row.combined_dir),""",
        1,
    )
    prepare["parameters"]["jsCode"] = prepare_code

    for name, persona in PERSONAS.items():
        evaluator = node(workflow, name)
        code = evaluator["parameters"]["jsCode"]
        marker = "const http = this.helpers.httpRequest.bind(this);"
        code = code.replace(
            marker,
            f"const PERSONA = {json.dumps(persona)};\n{marker}",
            1,
        )
        code = code.replace(
            "payload.systemPrompt",
            "(payload.systemPrompt + '\\n\\n' + PERSONA)",
        )
        if name == "Claude Evaluator":
            code = code.replace(
                "if (!apiKey) return fallback(payload.ticker, 'API key missing');",
                """if (!apiKey) return {
    ticker: payload.ticker, verdict: 'CLEAR', confidence: 50,
    upgrade_signals: [], red_flags: ['claude_unavailable'],
    reason: 'Claude API key not configured', force_skip: false,
    size_view: 'full', _model: 'claude'
  };""",
            )
        evaluator["parameters"]["jsCode"] = code

    verdict = node(workflow, "Verdict Node")
    code = verdict["parameters"]["jsCode"]
    code = code.replace(
        "const all_flags = [...new Set(models.flatMap(m => m.red_flags || []))];",
        """const all_flags = [...new Set(models.flatMap(m => m.red_flags || []))];
  const available = models.filter(m =>
    !(m.red_flags || []).includes('claude_unavailable') &&
    !(m.red_flags || []).includes('parse_error')
  ).length;""",
    )
    code = code.replace(
        "upgrade_signals: all_upgrade, all_red_flags: all_flags, allRedFlags: all_flags,",
        """upgrade_signals: all_upgrade, council_upgrade_sigs: all_upgrade,
    all_red_flags: all_flags, allRedFlags: all_flags,
    council_red_flags: all_flags, models_available: available,""",
    )
    verdict["parameters"]["jsCode"] = code

    risk = node(workflow, "Risk Engine")
    risk_code = risk["parameters"]["jsCode"]
    risk_code = risk_code.replace(
        "const MAX_TRADES = Number($vars.MAX_TRADES || 30);",
        "const MAX_TRADES = Number($vars.MAX_TRADES || 3);",
    )
    risk["parameters"]["jsCode"] = risk_code

    Path(args.output).write_text(
        json.dumps(workflow, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps({
        "output": args.output,
        "id": workflow.get("id"),
        "name": workflow["name"],
        "active": workflow["active"],
        "schedule": "30 3 * * 2-6",
        "council_gates_trades": False,
    }, indent=2))


if __name__ == "__main__":
    main()
