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
DEFAULT_URL = os.environ.get("OPTIONS_FLOW_URL", "http://72.62.134.167:8002/options-flow")


def _load_implied_options(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tickers = data.get("tickers") or {}
        return {str(k).upper(): v for k, v in tickers.items() if isinstance(v, dict)}
    except Exception:
        return {}


def _implied_verdict(pcr: float | None) -> str:
    if pcr is None:
        return "NO_DATA"
    if pcr < 0.7:
        return "CONFIRM"
    if pcr > 1.2:
        return "CAUTION"
    return "NEUTRAL"


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input", default=str(INPUT_PATH))
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--metadata", default=str(META_PATH))
    parser.add_argument("--implied-options", default=str(IMPLIED_OPTIONS_PATH), help="Path to implied_options_scraper.py output (options_flow.json).")
    parser.add_argument("--allow-no-data", action="store_true", help="Do not fail if service is unavailable.")
    args = parser.parse_args()

    setups = json.loads(Path(args.input).read_text(encoding="utf-8"))
    tickers = [s["symbol"] for s in setups]
    prices = {s["symbol"]: float(s.get("price") or 0) for s in setups}
    setup_context = {
        s["symbol"]: {
            "setup_score": s.get("setupQualityScore"),
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

    implied_options = _load_implied_options(Path(args.implied_options or IMPLIED_OPTIONS_PATH))

    by_ticker = {r.get("ticker"): r for r in results if r.get("ticker")}
    enriched = []
    for setup in setups:
        symbol = setup["symbol"]
        opt = by_ticker.get(symbol) or blank_result(symbol, "missing options result")
        imp = implied_options.get(symbol.upper(), {})

        service_verdict = opt.get("options_verdict")
        service_signals = opt.get("signals_count") or 0
        pcr = opt.get("put_call_vol_ratio") if opt.get("put_call_vol_ratio") is not None else imp.get("put_call_vol_ratio")
        iv_rank = opt.get("iv_rank_proxy") if opt.get("iv_rank_proxy") is not None else imp.get("iv_rank")

        # Fall back to ImpliedOptions summary PCR when the chain-derived service
        # has no directional signal (common when there are no UOA rows).
        if service_verdict in (None, "NEUTRAL", "NO_DATA") and pcr is not None:
            final_verdict = _implied_verdict(pcr)
            notes = opt.get("notes") or f"implied-options PCR {pcr}"
        else:
            final_verdict = service_verdict or "NO_DATA"
            notes = opt.get("notes")

        enriched.append({
            **setup,
            **opt,
            "options": {**opt, "implied_options": imp},
            "options_verdict": final_verdict,
            "options_signals_count": service_signals,
            "iv_rank_proxy": iv_rank,
            "call_vol_oi_ratio": opt.get("call_vol_oi_ratio") if opt.get("call_vol_oi_ratio") is not None else imp.get("call_vol_oi_ratio"),
            "put_call_vol_ratio": pcr,
            "call_oi_skew": opt.get("call_oi_skew") if opt.get("call_oi_skew") is not None else imp.get("call_oi_skew"),
            "options_notes": notes,
            "impliedoptions_iv_rank": imp.get("iv_rank"),
            "impliedoptions_put_call_vol_ratio": imp.get("put_call_vol_ratio"),
            "impliedoptions_verdict": _implied_verdict(imp.get("put_call_vol_ratio")),
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
        "error": error,
        "verdictCounts": {},
        "signalsCountDistribution": {},
        "loggingOnly": True,
        "selectionInfluence": "confluence rerank within fixed top-40 only; cannot create trades or change size",
        "notes": [
            "C1 options fields are attached for logging only.",
            "This stage cannot change membership or size; approved v2 contributions may re-rank within the fixed top-40 during confluence.",
            "Only the Stage 3 news-safe top-40 are sent to Yahoo/yfinance; never the full universe.",
        ],
    }
    for row in enriched:
        verdict = row.get("options_verdict") or "NO_DATA"
        metadata["verdictCounts"][verdict] = metadata["verdictCounts"].get(verdict, 0) + 1
        sc = str(row.get("options_signals_count"))
        metadata["signalsCountDistribution"][sc] = metadata["signalsCountDistribution"].get(sc, 0) + 1
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
