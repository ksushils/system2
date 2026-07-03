#!/usr/bin/env python3
"""Set 2 Options Flow universe builder.

Reads options flow data and Barchart UOA, scores candidates by flow signals,
and emits set2_candidates.json for downstream Set 2 scoring.

Paper mode only."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
OPTIONS_FLOW_PATH = ROOT / "options_flow.json"
BARCHART_PATH = ROOT / "data" / "barchart_uoa.json"
OUTPUT_PATH = ROOT / "data" / "set2_candidates.json"
LOG_DIR = ROOT / "logs"

# Known index ETFs to exclude from Barchart candidates
INDEX_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "VIX", "UVXY", "SPX", "NDX", "RUT",
    "XLK", "XLF", "XLE", "XLI", "XLP", "XLB", "XLU", "XLY", "XLC", "XLRE", "XLV",
}


def _log(message: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {message}", flush=True)


def today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def is_today_file(path: Path, date_field: str | None = "date") -> bool:
    """Return True if the JSON file exists and is from today."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if date_field and isinstance(data, dict) and data.get(date_field):
            file_date = str(data[date_field])[:10]
            return file_date == today_str()
    except Exception:
        pass
    # Fallback to mtime
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
        return mtime == datetime.now(timezone.utc).date()
    except Exception:
        return False


def load_options_flow() -> dict[str, dict[str, Any]]:
    if not is_today_file(OPTIONS_FLOW_PATH, date_field="run_date"):
        _log(f"options_flow.json missing or stale (path={OPTIONS_FLOW_PATH})")
        return {}
    try:
        data = json.loads(OPTIONS_FLOW_PATH.read_text(encoding="utf-8"))
        tickers = data.get("tickers", {})
        _log(f"Loaded options_flow.json with {len(tickers)} tickers")
        return tickers if isinstance(tickers, dict) else {}
    except Exception as exc:
        _log(f"ERROR reading options_flow.json: {exc}")
        return {}


def detect_barchart_schema(candidates: list[dict]) -> dict[str, str]:
    """Detect field name variants in Barchart records and return internal->actual mapping."""
    if not candidates:
        return {}
    sample = candidates[0]
    mapping: dict[str, str] = {}

    # symbol vs ticker
    mapping["symbol"] = "symbol" if "symbol" in sample else "ticker" if "ticker" in sample else ""
    # vol_oi_ratio vs volume_oi_ratio
    mapping["vol_oi_ratio"] = (
        "vol_oi_ratio" if "vol_oi_ratio" in sample
        else "volume_oi_ratio" if "volume_oi_ratio" in sample
        else "volumeOpenInterestRatio" if "volumeOpenInterestRatio" in sample
        else ""
    )
    # underlying_price vs stock_price
    mapping["underlying_price"] = (
        "underlying_price" if "underlying_price" in sample
        else "stock_price" if "stock_price" in sample
        else "lastPrice" if "lastPrice" in sample
        else ""
    )
    # days_to_expiry vs dte
    mapping["days_to_expiry"] = (
        "days_to_expiry" if "days_to_expiry" in sample
        else "dte" if "dte" in sample
        else "daysToExpiration" if "daysToExpiration" in sample
        else "expiration" if "expiration" in sample
        else ""
    )
    # option_type vs type
    mapping["option_type"] = (
        "option_type" if "option_type" in sample
        else "type" if "type" in sample
        else "optionType" if "optionType" in sample
        else ""
    )

    _log(f"Barchart schema detected: {mapping}")
    return mapping


def load_barchart_candidates() -> tuple[list[dict], bool]:
    if not BARCHART_PATH.exists():
        _log(f"barchart_uoa.json missing (path={BARCHART_PATH})")
        return [], False
    try:
        data = json.loads(BARCHART_PATH.read_text(encoding="utf-8"))
        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        if not isinstance(candidates, list):
            candidates = []
        _log(f"Loaded barchart_uoa.json with {len(candidates)} candidates")
        return candidates, True
    except Exception as exc:
        _log(f"ERROR reading barchart_uoa.json: {exc}")
        return [], False


def score_options_flow(ticker: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Score a single ticker from options_flow.json. Returns candidate dict or None."""
    score = 0
    notes: list[str] = []

    sweeps = int(data.get("ask_side_sweep_count") or 0)
    if sweeps >= 3:
        score += 35
        notes.append(f"{sweeps} ask-side sweeps")
    elif sweeps >= 1:
        score += 20
        notes.append(f"{sweeps} sweep(s)")

    bullish = float(data.get("bullish_premium_total") or 0)
    if bullish > 500_000:
        score += 25
        notes.append(f"${bullish/1e3:.0f}k bullish premium")
    elif bullish > 250_000:
        score += 15
        notes.append(f"${bullish/1e3:.0f}k bullish premium")
    elif bullish > 100_000:
        score += 8
        notes.append(f"${bullish/1e3:.0f}k bullish premium")

    repeat = int(data.get("repeat_flow_count") or 0)
    if repeat >= 5:
        score += 20
        notes.append(f"{repeat}x repeat flow")
    elif repeat >= 3:
        score += 12
        notes.append(f"{repeat}x repeat flow")

    pcr = float(data.get("put_call_vol_ratio") or 0)
    if pcr > 0 and pcr < 0.5:
        score += 10
        notes.append(f"PC ratio {pcr:.2f}")

    iv_rank = data.get("iv_rank")
    if isinstance(iv_rank, (int, float)):
        if 30 <= iv_rank <= 70:
            score += 5
            notes.append(f"IV rank {iv_rank}")
        elif iv_rank > 80:
            score -= 10
            notes.append(f"IV rank {iv_rank} (expensive)")

    bearish = float(data.get("bearish_premium_total") or 0)
    if bearish > 0 and bullish > 0 and bearish > bullish * 2:
        score -= 20
        notes.append("bearish premium dominant")

    qualifying = int(data.get("uoa_qualifying_rows") or 0)

    # Hard reject conditions
    if bullish <= 0:
        return None
    if qualifying < 1:
        return None
    if score < 35:
        return None

    # Additional hard reject: price/volume/earnings will be checked in scorer
    # We only have price here from options flow
    price = float(data.get("underlying_price") or 0)
    if price > 0 and (price < 10 or price > 2000):
        return None

    return {
        "ticker": ticker,
        "set": 2,
        "set_source": "options_flow",
        "source": "set2_options_flow",
        "sub_type": "options_flow_led",
        "set2_options_score": score,
        "multi_source_flow": False,
        "ask_side_sweep_count": sweeps,
        "bullish_premium_total": bullish,
        "bearish_premium_total": bearish,
        "repeat_flow_count": repeat,
        "put_call_vol_ratio": pcr,
        "iv_rank": iv_rank,
        "uoa_qualifying_rows": qualifying,
        "options_flow_source": data.get("source") or "impliedoptions_auth",
        "catalyst_summary": "Institutional call activity: " + ("; ".join(notes) if notes else "flow detected"),
        "date": today_str(),
        "set2_entry_rules": {
            "hold_days_max": 5,
            "size_multiplier": 0.75,
        },
        "_raw_price": price,
    }


def filter_barchart_candidate(record: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any] | None:
    """Convert a Barchart record to Set 2 candidate format if it passes filters."""
    sym = str(record.get(mapping.get("symbol", "symbol")) or "").upper().strip()
    if not sym or sym in INDEX_ETFS:
        return None

    opt_type = str(record.get(mapping.get("option_type", "option_type")) or "").upper()
    if opt_type not in {"CALL", "C"}:
        return None

    vol_oi_field = mapping.get("vol_oi_ratio", "vol_oi_ratio")
    vol_oi = float(record.get(vol_oi_field) or 0)
    if vol_oi <= 2.5:
        return None

    price_field = mapping.get("underlying_price", "underlying_price")
    price = float(record.get(price_field) or 0)
    if price <= 15:
        return None

    dte_field = mapping.get("days_to_expiry", "days_to_expiry")
    if dte_field == "expiration":
        from datetime import date, datetime
        try:
            exp = datetime.strptime(str(record.get("expiration")), "%Y-%m-%d").date()
            dte = (exp - date.today()).days
        except Exception:
            dte = 0
    else:
        dte = int(record.get(dte_field) or 0)
    if not (4 <= dte <= 45):
        return None

    return {
        "ticker": sym,
        "set": 2,
        "set_source": "options_flow",
        "source": "set2_options_flow",
        "sub_type": "barchart_uoa",
        "set2_options_score": min(70, int(45 + vol_oi)),  # baseline + vol/OI bonus
        "multi_source_flow": False,
        "ask_side_sweep_count": 0,
        "bullish_premium_total": 0,
        "bearish_premium_total": 0,
        "repeat_flow_count": 0,
        "put_call_vol_ratio": 0.0,
        "iv_rank": None,
        "uoa_qualifying_rows": 1,
        "options_flow_source": "barchart_uoa",
        "catalyst_summary": f"Barchart UOA: call vol/OI {vol_oi:.1f}x, DTE {dte}",
        "date": today_str(),
        "set2_entry_rules": {
            "hold_days_max": 5,
            "size_multiplier": 0.75,
        },
        "_raw_price": price,
        "_barchart_dte": dte,
    }


def deduplicate_and_merge(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """If same ticker appears multiple times, merge and mark multi_source_flow."""
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        by_ticker.setdefault(c["ticker"], []).append(c)

    merged: list[dict[str, Any]] = []
    for ticker, items in by_ticker.items():
        if len(items) == 1:
            merged.append(items[0])
            continue
        # Merge: take highest score, sum flow signals, mark multi-source
        best = max(items, key=lambda x: x.get("set2_options_score", 0))
        result = dict(best)
        result["multi_source_flow"] = True
        result["sub_type"] = "multi_source_flow"
        result["catalyst_summary"] = "Multi-source flow: " + " | ".join(
            c.get("catalyst_summary", "") for c in items
        )
        merged.append(result)

    return merged


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log("Set 2 universe builder started")

    run_id = os.environ.get("SYSTEM2_RUN_ID") or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(4).hex()
    )

    candidates: list[dict[str, Any]] = []

    # Primary source: options_flow.json
    flow_tickers = load_options_flow()
    flow_count = 0
    for ticker, data in flow_tickers.items():
        candidate = score_options_flow(ticker, data)
        if candidate:
            candidates.append(candidate)
            flow_count += 1
    _log(f"Options flow candidates: {flow_count}")

    # Secondary source: Barchart UOA
    barchart_records, barchart_available = load_barchart_candidates()
    schema_mapping = detect_barchart_schema(barchart_records)
    barchart_count = 0
    if barchart_records and schema_mapping.get("symbol"):
        for record in barchart_records:
            candidate = filter_barchart_candidate(record, schema_mapping)
            if candidate:
                candidates.append(candidate)
                barchart_count += 1
    _log(f"Barchart candidates: {barchart_count} (schema={schema_mapping})")

    # Deduplicate
    candidates = deduplicate_and_merge(candidates)
    _log(f"Total unique candidates after dedup: {len(candidates)}")

    payload = {
        "date": today_str(),
        "run_id": run_id,
        "candidate_count": len(candidates),
        "options_flow_count": flow_count,
        "barchart_count": barchart_count,
        "barchart_available": barchart_available,
        "barchart_schema": schema_mapping,
        "candidates": candidates,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(OUTPUT_PATH)
    _log(f"Wrote {len(candidates)} candidates to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
