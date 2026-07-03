#!/usr/bin/env python3
"""Merge Set 1, Set 2, and Set 3 ideas before B4 cluster guard.

Reads the best available Set 1 output (council → confluence → stage4 → stage2)
and merges it with Set 2 and Set 3 scored ideas. Writes back to the same Set 1 path
so B4 automatically reads the merged list.

Paper mode only. Set 3 is staged/preview — merge supports it but pipeline
orchestrator does NOT yet call it automatically.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SET2_PATH = ROOT / "data" / "set2_scored.json"
SET3_PATH = ROOT / "data" / "set3_scored.json"
MERGED_LOG_PATH = ROOT / "data" / "merged_sets.json"

# Set 1 input candidates in order of preference (same as B4)
SET1_CANDIDATES = [
    ROOT / "stage6_council_enriched.json",
    ROOT / "stage2_confluence_ranked_top40.json",
    ROOT / "stage4_options_enriched_top40.json",
    ROOT / "stage2_surgical_strike_top40.json",
]


def _log(message: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {message}", flush=True)


def find_set1_path() -> Path | None:
    for path in SET1_CANDIDATES:
        if path.exists():
            return path
    return None


def load_set1(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("ideas") or data.get("candidates") or data.get("results") or []
    except Exception as exc:
        _log(f"ERROR reading Set 1 from {path}: {exc}")
    return []


def load_set2() -> list[dict[str, Any]]:
    if not SET2_PATH.exists():
        _log(f"Set 2 file missing: {SET2_PATH}")
        return []
    try:
        data = json.loads(SET2_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            candidates = data.get("candidates") or data.get("ideas") or []
            _log(f"Loaded Set 2 with {len(candidates)} candidates")
            return candidates if isinstance(candidates, list) else []
    except Exception as exc:
        _log(f"ERROR reading Set 2: {exc}")
    return []


def load_set3() -> list[dict[str, Any]]:
    if not SET3_PATH.exists():
        _log(f"Set 3 file missing: {SET3_PATH}")
        return []
    try:
        data = json.loads(SET3_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # Stale-date guard: only use Set 3 if generated today
            set3_date = str(data.get("date", "")).strip()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if set3_date and set3_date != today:
                _log(f"Set 3 data is stale (dated {set3_date}) — skipping")
                return []
            candidates = data.get("candidates") or data.get("ideas") or []
            _log(f"Loaded Set 3 with {len(candidates)} candidates (STAGED)")
            return candidates if isinstance(candidates, list) else []
    except Exception as exc:
        _log(f"ERROR reading Set 3: {exc}")
    return []


def add_set_field_to_set1(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure all Set 1 records have set=1 if not already present."""
    out: list[dict[str, Any]] = []
    for row in rows:
        enriched = dict(row)
        if enriched.get("set") is None:
            enriched["set"] = 1
            enriched["set_source"] = "technical_momentum"
        out.append(enriched)
    return out


def _merge_set2_fields(base: dict[str, Any], s2: dict[str, Any]) -> None:
    base["set2_options_score"] = s2.get("set2_options_score")
    base["set2_trade_quality_score"] = s2.get("set2_trade_quality_score")
    base["set2_core_score"] = s2.get("set2_core_score")
    base["set2_risk_score"] = s2.get("set2_risk_score")
    base["ask_side_sweep_count"] = s2.get("ask_side_sweep_count", 0)
    base["bullish_premium_total"] = s2.get("bullish_premium_total", 0)
    base["repeat_flow_count"] = s2.get("repeat_flow_count", 0)
    base["catalyst_summary"] = s2.get("catalyst_summary", "")
    base["multi_source_flow"] = s2.get("multi_source_flow", False)


def _merge_set3_fields(base: dict[str, Any], s3: dict[str, Any]) -> None:
    base["set3_catalyst_score"] = s3.get("set3_catalyst_score")
    base["set3_trade_quality_score"] = s3.get("set3_trade_quality_score")
    base["set3_core_score"] = s3.get("set3_core_score")
    base["set3_risk_score"] = s3.get("set3_risk_score")
    base["set3_directional_bias"] = s3.get("set3_directional_bias")
    base["set3_bias_confidence"] = s3.get("set3_bias_confidence")
    base["catalyst_summary"] = s3.get("catalyst_summary", base.get("catalyst_summary", ""))
    base["catalyst_date"] = s3.get("catalyst_date")
    base["catalyst_datetime"] = s3.get("catalyst_datetime")
    base["catalyst_sub_types"] = s3.get("catalyst_sub_types", [])
    base["catalyst_sources"] = s3.get("catalyst_sources", [])
    base["price_target"] = s3.get("price_target")
    base["analyst_company"] = s3.get("analyst_company")
    base["news_url"] = s3.get("news_url")
    base["publisher"] = s3.get("publisher")
    base["earnings_surprise_pct"] = s3.get("earnings_surprise_pct")
    base["insider_buy_count"] = s3.get("insider_buy_count")
    base["insider_buy_value"] = s3.get("insider_buy_value")
    base["bypasses_technical"] = s3.get("bypasses_technical", False)
    base["bypass_technical"] = s3.get("bypass_technical", False)
    base["bypass_reason"] = s3.get("bypass_reason", "")


def merge_ideas(set1: list[dict[str, Any]], set2: list[dict[str, Any]],
                set3: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge Set 1, Set 2, and Set 3 ideas, handling duplicates across all sets."""
    set1_by_symbol: dict[str, dict[str, Any]] = {}
    for row in set1:
        sym = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
        if sym:
            set1_by_symbol[sym] = row

    set2_by_symbol: dict[str, dict[str, Any]] = {}
    for row in set2:
        sym = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
        if sym:
            set2_by_symbol[sym] = row

    set3_by_symbol: dict[str, dict[str, Any]] = {}
    for row in set3:
        sym = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
        if sym:
            set3_by_symbol[sym] = row

    merged: list[dict[str, Any]] = []
    counts = {
        "set1_count": len(set1_by_symbol),
        "set2_count": len(set2_by_symbol),
        "set3_count": len(set3_by_symbol),
        "multi_set_count": 0,
        "multi_12_count": 0,
        "multi_13_count": 0,
        "multi_23_count": 0,
        "multi_123_count": 0,
        "set1_only_count": 0,
        "set2_only_count": 0,
        "set3_only_count": 0,
        "total_count": 0,
    }

    # All unique symbols across all sets
    all_symbols = set(set1_by_symbol.keys()) | set(set2_by_symbol.keys()) | set(set3_by_symbol.keys())

    for sym in sorted(all_symbols):
        in1 = sym in set1_by_symbol
        in2 = sym in set2_by_symbol
        in3 = sym in set3_by_symbol

        # Choose base: prefer Set 1 > Set 2 > Set 3
        if in1:
            base = dict(set1_by_symbol[sym])
        elif in2:
            base = dict(set2_by_symbol[sym])
        else:
            base = dict(set3_by_symbol[sym])

        sets_present = []
        if in1:
            sets_present.append(1)
        if in2:
            sets_present.append(2)
        if in3:
            sets_present.append(3)

        if len(sets_present) >= 2:
            base["multi_set_idea"] = True
            base["multi_set_sets"] = sets_present
            counts["multi_set_count"] += 1
            if sets_present == [1, 2]:
                counts["multi_12_count"] += 1
            elif sets_present == [1, 3]:
                counts["multi_13_count"] += 1
            elif sets_present == [2, 3]:
                counts["multi_23_count"] += 1
            elif len(sets_present) == 3:
                counts["multi_123_count"] += 1

            # Confluence bonus: +8 for any 2-set overlap, +12 for triple
            bonus = 12 if len(sets_present) == 3 else 8
            tq = base.get("trade_quality_score") or base.get("setupQualityScore") or 0
            base["trade_quality_score"] = min(100, tq + bonus)
            base["multi_set_bonus"] = bonus

        if in2:
            _merge_set2_fields(base, set2_by_symbol[sym])
        if in3:
            _merge_set3_fields(base, set3_by_symbol[sym])

        if in1 and not in2 and not in3:
            counts["set1_only_count"] += 1
        elif in2 and not in1 and not in3:
            counts["set2_only_count"] += 1
        elif in3 and not in1 and not in2:
            counts["set3_only_count"] += 1

        merged.append(base)

    counts["total_count"] = len(merged)
    return merged, counts


def main() -> int:
    _log("Merge sets started")

    set1_path = find_set1_path()
    if not set1_path:
        _log("PIPELINE FAILURE: no Set 1 input file found")
        return 1

    _log(f"Set 1 input: {set1_path}")
    set1 = load_set1(set1_path)
    if not set1:
        _log("PIPELINE FAILURE: Set 1 input is empty")
        return 1

    set1 = add_set_field_to_set1(set1)
    set2 = load_set2()
    set3 = load_set3()

    sets_available = {
        "set1": True,
        "set2": bool(set2),
        "set3": bool(set3),
    }

    if not set2 and not set3:
        _log("Set 2 and Set 3 unavailable; using Set 1 only")
        merged = set1
        counts = {
            "set1_count": len(set1),
            "set2_count": 0,
            "set3_count": 0,
            "multi_set_count": 0,
            "multi_12_count": 0,
            "multi_13_count": 0,
            "multi_23_count": 0,
            "multi_123_count": 0,
            "set1_only_count": len(set1),
            "set2_only_count": 0,
            "set3_only_count": 0,
            "total_count": len(set1),
        }
    else:
        merged, counts = merge_ideas(set1, set2, set3)

    run_id = os.environ.get("SYSTEM2_RUN_ID") or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(4).hex()
    )

    # Write merged list back to the Set 1 path so B4 reads it
    set1_path.write_text(json.dumps(merged, indent=2, default=str), encoding="utf-8")
    _log(f"Wrote merged {counts['total_count']} ideas back to {set1_path}")

    # Also write to merged_sets.json for logging/auditing
    merged_payload = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "run_id": run_id,
        "set1_path": str(set1_path),
        "set2_path": str(SET2_PATH),
        "set3_path": str(SET3_PATH),
        "sets_available": sets_available,
        **counts,
        "ideas": merged,
    }
    MERGED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MERGED_LOG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged_payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(MERGED_LOG_PATH)
    _log(f"Wrote merged log to {MERGED_LOG_PATH}")

    print(json.dumps({"stage": "merge_sets", **counts, **sets_available}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
