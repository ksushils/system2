#!/usr/bin/env python3
"""
C1 Stage 4 options-flow ride-along.

Reads Stage 2 top-40, calls the options-flow service, and attaches:
  options_verdict, options_signals_count, iv_rank_proxy,
  call_vol_oi_ratio, put_call_vol_ratio, call_oi_skew

Important: this stage cannot change membership or position size. It preserves
the fixed Stage 3 survivor set; approved v2 contributions may later re-rank
those same names inside confluence.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INPUT_PATH = ROOT / "stage3_news_safe_top40.json"
OUTPUT_PATH = ROOT / "stage4_options_enriched_top40.json"
META_PATH = ROOT / "stage4_options_metadata.json"
IMPLIED_OPTIONS_PATH = ROOT / "options_flow.json"
IMPLIED_OPTIONS_META_PATH = ROOT / "options_flow.metadata.json"
DEFAULT_URL = os.environ.get("OPTIONS_FLOW_URL", "http://72.62.134.167:8002/options-flow")


def post_options_flow(url: str, tickers: list[str], prices: dict[str, float], setups: dict[str, dict], timeout: int = 900) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps({"tickers": tickers, "prices": prices, "setups": setups}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "system2-c1-stage4/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def blank_result(ticker: str, notes: str) -> dict:
    return {
        "ticker": ticker,
        "iv_atm": None,
        "iv_rank_proxy": None,
        "call_vol_oi_ratio": None,
        "put_call_vol_ratio": None,
        "call_oi_skew": None,
        "signals_count": 0,
        "options_verdict": "NO_DATA",
        "notes": notes,
    }


def score_implied_options(implied: dict, yahoo: dict, authenticated: bool) -> dict:
    if not implied:
        return {
            **yahoo,
            "data_source": "yahoo_fallback" if yahoo.get("options_verdict") != "NO_DATA" else "unavailable",
            "options_score": None,
            "options_score_reasons": [],
            "options_verdict_old_yahoo": yahoo.get("options_verdict"),
        }

    score = 0
    reasons = []
    ask_sweeps = int(implied.get("ask_side_sweep_count") or 0)
    bullish_premium = float(implied.get("bullish_premium_total") or 0)
    bearish_premium = float(implied.get("bearish_premium_total") or 0)
    repeat_flow = int(implied.get("repeat_flow_count") or 0)
    qualifying_rows = int(implied.get("uoa_qualifying_rows") or 0)
    call_ratio = implied.get("call_vol_oi_ratio")
    if call_ratio is None:
        call_ratio = yahoo.get("call_vol_oi_ratio")
    put_call_ratio = implied.get("put_call_vol_ratio")
    iv_rank = implied.get("iv_rank")

    if qualifying_rows == 0:
        return {
            **yahoo,
            "iv_atm": implied.get("atm_iv"),
            "iv_rank_proxy": implied.get("iv_rank"),
            "call_vol_oi_ratio": call_ratio,
            "put_call_vol_ratio": put_call_ratio,
            "signals_count": 0,
            "options_verdict": "NEUTRAL",
            "notes": "ImpliedOptions primary; no qualifying non-0DTE UOA rows",
            "data_source": "impliedoptions_auth" if authenticated else "impliedoptions_unauth",
            "options_score": 0,
            "options_score_reasons": [{"name": "no_qualifying_uoa_rows", "value": 0}],
            "options_verdict_old_yahoo": yahoo.get("options_verdict"),
        }

    if ask_sweeps >= 1:
        score += 3
        reasons.append({"name": "ask_side_sweep", "value": 3})
    if bullish_premium > 100_000:
        score += 3
        reasons.append({"name": "bullish_premium_over_100k", "value": 3})
    if repeat_flow >= 3:
        score += 3
        reasons.append({"name": "repeat_flow_3plus", "value": 3})
    if call_ratio is not None and float(call_ratio) > 1.5:
        score += 2
        reasons.append({"name": "call_vol_oi_over_1_5", "value": 2})
    if put_call_ratio is not None and float(put_call_ratio) < 0.7:
        score += 2
        reasons.append({"name": "put_call_volume_below_0_7", "value": 2})
    if iv_rank is not None and 40 <= float(iv_rank) <= 75:
        score += 2
        reasons.append({"name": "iv_rank_healthy_40_75", "value": 2})
    if bearish_premium > bullish_premium and ask_sweeps == 0:
        score -= 2
        reasons.append({"name": "bearish_premium_dominates", "value": -2})

    verdict = "STRONG_CONFIRM" if score >= 5 else "CONFIRM" if score >= 3 else "NEUTRAL" if score >= 1 else "CAUTION"
    return {
        **yahoo,
        "iv_atm": implied.get("atm_iv"),
        "iv_rank_proxy": implied.get("iv_rank"),
        "call_vol_oi_ratio": call_ratio,
        "put_call_vol_ratio": put_call_ratio,
        "signals_count": len(reasons),
        "options_verdict": verdict,
        "notes": "ImpliedOptions primary; Yahoo retained as fallback/comparison",
        "data_source": "impliedoptions_auth" if authenticated else "impliedoptions_unauth",
        "options_score": score,
        "options_score_reasons": reasons,
        "options_verdict_old_yahoo": yahoo.get("options_verdict"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input", default=str(INPUT_PATH))
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--metadata", default=str(META_PATH))
    parser.add_argument("--implied-options", default=str(IMPLIED_OPTIONS_PATH))
    parser.add_argument("--allow-no-data", action="store_true", help="Do not fail if service is unavailable.")
    args = parser.parse_args()

    setups = json.loads(Path(args.input).read_text(encoding="utf-8"))
    tickers = [s["symbol"] for s in setups]
    prices = {s["symbol"]: float(s.get("price") or 0) for s in setups}
    setup_context = {
        s["symbol"]: {
            "setup_score": s.get("setupQualityScore"),
            "tp1": s.get("tp1"),
            "stopLoss": s.get("stopLoss"),
            "atr14": s.get("atr14"),
            "sector": s.get("sector"),
            "regime": os.environ.get("SYSTEM2_REGIME"),
        }
        for s in setups
    }

    error = None
    try:
        response = post_options_flow(args.url, tickers, prices, setup_context)
        results = response.get("results", [])
    except Exception as exc:
        error = str(exc)
        if not args.allow_no_data:
            raise
        response = {"ok": False, "error": error, "results": []}
        results = [blank_result(t, f"options service unavailable: {error}") for t in tickers]

    by_ticker = {r.get("ticker"): r for r in results if r.get("ticker")}
    try:
        implied_options = json.loads(Path(args.implied_options).read_text(encoding="utf-8"))
        implied_authenticated = implied_options.get("source") == "impliedoptions_authenticated"
        if isinstance(implied_options.get("tickers"), dict):
            implied_options = implied_options["tickers"]
    except Exception:
        implied_options = {}
        implied_authenticated = False
    enriched = []
    for setup in setups:
        symbol = setup["symbol"]
        yahoo_opt = by_ticker.get(symbol) or blank_result(symbol, "missing options result")
        implied = implied_options.get(symbol) or {}
        opt = score_implied_options(implied, yahoo_opt, implied_authenticated)
        enriched.append({
            **setup,
            **opt,
            "options": opt,
            "options_verdict": opt.get("options_verdict"),
            "options_signals_count": opt.get("signals_count"),
            "iv_rank_proxy": opt.get("iv_rank_proxy"),
            "call_vol_oi_ratio": opt.get("call_vol_oi_ratio"),
            "put_call_vol_ratio": opt.get("put_call_vol_ratio"),
            "call_oi_skew": opt.get("call_oi_skew"),
            "options_notes": opt.get("notes"),
            "options_data_source": opt.get("data_source"),
            "options_score": opt.get("options_score"),
            "options_score_reasons": opt.get("options_score_reasons") or [],
            "options_verdict_old_yahoo": opt.get("options_verdict_old_yahoo"),
            "impliedoptions_available": bool(implied),
            "impliedoptions_iv_rank": implied.get("iv_rank"),
            "impliedoptions_iv_percentile": implied.get("iv_percentile"),
            "impliedoptions_atm_iv": implied.get("atm_iv"),
            "impliedoptions_call_volume": implied.get("total_call_volume"),
            "impliedoptions_put_volume": implied.get("total_put_volume"),
            "impliedoptions_put_call_vol_ratio": implied.get("put_call_vol_ratio"),
            "impliedoptions_call_vol_oi_ratio": implied.get("call_vol_oi_ratio"),
            "impliedoptions_uoa": implied.get("uoa") or [],
            "uoa_rows_today": int(implied.get("uoa_rows_today") or 0),
            "uoa_qualifying_rows": int(implied.get("uoa_qualifying_rows") or 0),
            "phase_c1_mode": "ride_along_logging_only",
        })

    metadata = {
        "stage": "STAGE4_OPTIONS",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "inputCount": len(setups),
        "outputCount": len(enriched),
        "serviceUrl": args.url,
        "serviceOk": bool(response.get("ok")),
        "serviceRuntimeSeconds": response.get("runtime_seconds"),
        "impliedOptionsPath": args.implied_options,
        "impliedOptionsAvailableCount": sum(
            bool(row.get("impliedoptions_available")) for row in enriched
        ),
        "dataSourceCounts": {},
        "oldYahooVerdictCounts": {},
        "error": error,
        "verdictCounts": {},
        "signalsCountDistribution": {},
        "loggingOnly": True,
        "selectionInfluence": "confluence rerank within fixed top-40 only; cannot create trades or change size",
        "notes": [
            "C1 options fields are attached for logging only.",
            "This stage cannot change membership or size; approved v2 contributions may re-rank within the fixed top-40 during confluence.",
            "Only the Stage 3 news-safe top-40 are sent to Yahoo/yfinance; never the full universe.",
            "ImpliedOptions values are attached as separate ride-along fields and do not change verdicts or scoring.",
        ],
    }
    for row in enriched:
        verdict = row.get("options_verdict") or "NO_DATA"
        metadata["verdictCounts"][verdict] = metadata["verdictCounts"].get(verdict, 0) + 1
        sc = str(row.get("options_signals_count"))
        metadata["signalsCountDistribution"][sc] = metadata["signalsCountDistribution"].get(sc, 0) + 1
        source = row.get("options_data_source") or "unavailable"
        metadata["dataSourceCounts"][source] = metadata["dataSourceCounts"].get(source, 0) + 1
        old_verdict = row.get("options_verdict_old_yahoo") or "NO_DATA"
        metadata["oldYahooVerdictCounts"][old_verdict] = metadata["oldYahooVerdictCounts"].get(old_verdict, 0) + 1
    metadata["confirm_count"] = metadata["verdictCounts"].get("CONFIRM", 0)
    metadata["neutral_count"] = metadata["verdictCounts"].get("NEUTRAL", 0)
    metadata["caution_count"] = metadata["verdictCounts"].get("CAUTION", 0)
    metadata["no_data_count"] = metadata["verdictCounts"].get("NO_DATA", 0)

    Path(args.output).write_text(json.dumps(enriched, indent=2), encoding="utf-8")
    Path(args.metadata).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps({
        "inputCount": metadata["inputCount"],
        "outputCount": metadata["outputCount"],
        "serviceOk": metadata["serviceOk"],
        "verdictCounts": metadata["verdictCounts"],
        "loggingOnly": True,
        "selectionInfluence": metadata["selectionInfluence"],
    }, indent=2))


if __name__ == "__main__":
    main()
