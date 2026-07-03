#!/usr/bin/env python3
"""
System 2 — Stage Alignment Guard.

Ensures enrichment stages (4, 5, 6) ran on the SAME symbol set as Stage 2.
Prevents scoring garbage finalists when pipeline stages drift or fail partway.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent

# File paths (same as used by confluence_scoring.py and the pipeline)
STAGE2_PATH = ROOT / "stage2_surgical_strike_top40.json"
STAGE4_PATH = ROOT / "stage4_options_enriched_top40.json"
STAGE5_PATH = ROOT / "stage5_combined_forecast_top40.json"
STAGE6_PATH = ROOT / "stage6_council_enriched.json"


def _symbols_from_json(path: Path) -> set[str]:
    """Load a list-of-dicts JSON and extract symbol/ticker fields."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return set()

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("finalists", data.get("tickers", []))
    else:
        return set()

    symbols: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            sym = row.get("symbol") or row.get("ticker")
            if sym:
                symbols.add(str(sym).strip().upper())
        elif isinstance(row, str):
            symbols.add(row.strip().upper())
    return symbols


@dataclass(frozen=True)
class AlignmentResult:
    ok: bool
    stage2_count: int
    stage4_count: int
    stage5_count: int
    stage6_count: int
    stage4_overlap_pct: float
    stage5_overlap_pct: float
    stage6_overlap_pct: float
    stage2_symbols: set[str]
    stage4_symbols: set[str]
    stage5_symbols: set[str]
    stage6_symbols: set[str]
    message: str


def check_stage_alignment(min_overlap_pct: float = 90.0) -> AlignmentResult:
    """
    Compare Stage 2 symbols against enrichment symbol sets.

    - Stage 4 and Stage 5 are REQUIRED (they run before confluence in the pipeline).
    - Stage 6 is OPTIONAL (council may legitimately be absent if all models abstained).
    """
    stage2 = _symbols_from_json(STAGE2_PATH)
    stage4 = _symbols_from_json(STAGE4_PATH)
    stage5 = _symbols_from_json(STAGE5_PATH)
    stage6 = _symbols_from_json(STAGE6_PATH)

    stage2_count = len(stage2)
    stage4_count = len(stage4)
    stage5_count = len(stage5)
    stage6_count = len(stage6)

    def overlap_pct(enrichment: set[str]) -> float:
        if not stage2:
            return 0.0
        if not enrichment:
            return 0.0
        return round(len(enrichment & stage2) / len(stage2) * 100, 1)

    s4_pct = overlap_pct(stage4)
    s5_pct = overlap_pct(stage5)
    s6_pct = overlap_pct(stage6)

    # Stage 4 and 5 are required enrichments before confluence
    present: list[tuple[str, float]] = []
    if stage4:
        present.append(("S4", s4_pct))
    if stage5:
        present.append(("S5", s5_pct))
    if stage6:
        present.append(("S6", s6_pct))

    if not stage2:
        return AlignmentResult(
            ok=False, stage2_count=0,
            stage4_count=stage4_count, stage5_count=stage5_count, stage6_count=stage6_count,
            stage4_overlap_pct=s4_pct, stage5_overlap_pct=s5_pct, stage6_overlap_pct=s6_pct,
            stage2_symbols=stage2, stage4_symbols=stage4, stage5_symbols=stage5, stage6_symbols=stage6,
            message="ALIGNMENT_FAILURE: Stage 2 file missing or empty. No source of truth.",
        )

    if not present:
        return AlignmentResult(
            ok=False, stage2_count=stage2_count,
            stage4_count=0, stage5_count=0, stage6_count=0,
            stage4_overlap_pct=0.0, stage5_overlap_pct=0.0, stage6_overlap_pct=0.0,
            stage2_symbols=stage2, stage4_symbols=set(), stage5_symbols=set(), stage6_symbols=set(),
            message="ALIGNMENT_FAILURE: No enrichment files found (Stage 4/5/6 all missing). "
                    "Pipeline may have failed before enrichment.",
        )

    failures: list[str] = []
    if stage4 and s4_pct < min_overlap_pct:
        failures.append(f"S4={s4_pct}%")
    if stage5 and s5_pct < min_overlap_pct:
        failures.append(f"S5={s5_pct}%")
    # Stage 6 is optional — only check if present AND grossly mismatched
    # (if council ran but on completely wrong symbols, that's a problem)
    if stage6 and s6_pct < min_overlap_pct:
        failures.append(f"S6={s6_pct}%")

    if failures:
        msg = (
            f"ALIGNMENT_FAILURE: Stage 2 had {stage2_count} symbols. "
            f"Enrichment overlap below {min_overlap_pct}%: {', '.join(failures)}. "
            f"Stage 4 matched {len(stage4 & stage2)}/{stage4_count}, "
            f"Stage 5 matched {len(stage5 & stage2)}/{stage5_count}, "
            f"Stage 6 matched {len(stage6 & stage2)}/{stage6_count}. "
            f"DO NOT SCORE — enrichment is stale or ran on wrong symbol set."
        )
        return AlignmentResult(
            ok=False, stage2_count=stage2_count,
            stage4_count=stage4_count, stage5_count=stage5_count, stage6_count=stage6_count,
            stage4_overlap_pct=s4_pct, stage5_overlap_pct=s5_pct, stage6_overlap_pct=s6_pct,
            stage2_symbols=stage2, stage4_symbols=stage4, stage5_symbols=stage5, stage6_symbols=stage6,
            message=msg,
        )

    msg = (
        f"Stage alignment OK: "
        f"S4={s4_pct}% ({len(stage4 & stage2)}/{stage2_count}) "
        f"S5={s5_pct}% ({len(stage5 & stage2)}/{stage2_count}) "
        f"S6={s6_pct}% ({len(stage6 & stage2)}/{stage2_count}) "
        f"— proceeding with scoring"
    )
    return AlignmentResult(
        ok=True, stage2_count=stage2_count,
        stage4_count=stage4_count, stage5_count=stage5_count, stage6_count=stage6_count,
        stage4_overlap_pct=s4_pct, stage5_overlap_pct=s5_pct, stage6_overlap_pct=s6_pct,
        stage2_symbols=stage2, stage4_symbols=stage4, stage5_symbols=stage5, stage6_symbols=stage6,
        message=msg,
    )


def per_idea_enrichment_check(row: dict[str, Any]) -> dict[str, Any]:
    """
    Belt-and-braces: even when overall alignment passes,
    skip bonuses for individual tickers missing Stage 4 or Stage 5 enrichment.
    """
    ticker = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
    if not ticker:
        return {"partial_enrichment": True, "enrichment_note": "Missing ticker"}

    stage4 = _symbols_from_json(STAGE4_PATH)
    stage5 = _symbols_from_json(STAGE5_PATH)

    missing: list[str] = []
    if ticker not in stage4:
        missing.append("Stage 4")
    if ticker not in stage5:
        missing.append("Stage 5")

    if missing:
        return {
            "partial_enrichment": True,
            "enrichment_note": f"{ticker} partial enrichment — bonuses skipped for missing: {', '.join(missing)}",
        }

    return {"partial_enrichment": False, "enrichment_note": None}


if __name__ == "__main__":
    import sys
    result = check_stage_alignment()
    print(result.message)
    if not result.ok:
        print(f"  Stage 2 ({result.stage2_count}): {sorted(result.stage2_symbols)[:10]}...")
        print(f"  Stage 4 ({result.stage4_count}): {sorted(result.stage4_symbols)[:10]}...")
        print(f"  Stage 5 ({result.stage5_count}): {sorted(result.stage5_symbols)[:10]}...")
        print(f"  Stage 6 ({result.stage6_count}): {sorted(result.stage6_symbols)[:10]}...")
        sys.exit(1)
