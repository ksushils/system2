#!/usr/bin/env python3
"""Read-only audits plus append-only outcome-version backfill for research V2."""

from __future__ import annotations

import argparse
import glob
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from research_telemetry_common import RESEARCH_ROOT, content_hash, read_json, version_resolved_outcomes

SCOREBOARD_PATTERNS = {
    "swing": "swing_outcomes_v2_*.json",
    "stage1_v2": "stage1_v2_outcomes_*.json",
    "challenger": "unchased_challenger_outcomes_v2_*.json",
    "momentum_acceleration_v2": "momentum_acceleration_outcomes_v2_*.json",
}


def backfill_versions() -> dict[str, Any]:
    report: dict[str, Any] = {"families": {}, "research_only": True, "broker_calls": 0}
    for family, pattern in SCOREBOARD_PATTERNS.items():
        totals = Counter()
        files = sorted((RESEARCH_ROOT / "scoreboards").glob(pattern))
        for path in files:
            payload = read_json(path, {}) or {}
            _, counters = version_resolved_outcomes(payload.get("rows", []), family, payload.get("created_at") or path.stem)
            totals.update(counters)
        report["families"][family] = {"snapshots": len(files), **dict(totals)}
    return report


def latest(pattern: str) -> Path | None:
    files = sorted((RESEARCH_ROOT / "scoreboards").glob(pattern))
    return files[-1] if files else None


def missing_reason(row: dict[str, Any], horizon: int) -> str | None:
    block = row.get(f"d{horizon}")
    if isinstance(block, dict) and block.get("state") != "AVAILABLE":
        return str(block.get("reason") or block.get("state") or "OTHER")
    if not isinstance(block, dict) and row.get("outcome_state") == "MISSING_PRICE":
        return str(row.get("missing_reason") or "OTHER")
    return None


def coverage_report() -> dict[str, Any]:
    output: dict[str, Any] = {"research_only": True, "broker_calls": 0, "families": {}}
    for family, pattern in SCOREBOARD_PATTERNS.items():
        path = latest(pattern)
        if not path:
            continue
        rows = (read_json(path, {}) or {}).get("rows", [])
        group_field = "challenger" if family == "challenger" else "cohort"
        groups: dict[str, Any] = {}
        for group in sorted({str(row.get(group_field) or "UNGROUPED") for row in rows}):
            members = [row for row in rows if str(row.get(group_field) or "UNGROUPED") == group]
            horizons: dict[str, Any] = {}
            for horizon in (1, 2, 3, 5, 7):
                available = sum((row.get(f"d{horizon}") or {}).get("state") == "AVAILABLE" for row in members)
                reasons = Counter(filter(None, (missing_reason(row, horizon) for row in members)))
                horizons[f"d{horizon}"] = {"available": available, "population": len(members), "missing_reasons": dict(reasons)}
            groups[group] = horizons
        output["families"][family] = {"artifact": str(path), "rows": len(rows), "groups": groups}
    return output


def verify() -> dict[str, Any]:
    report: dict[str, Any] = {"research_only": True, "broker_calls": 0, "families": {}}
    for family, pattern in SCOREBOARD_PATTERNS.items():
        path = latest(pattern)
        if not path:
            continue
        payload = read_json(path, {}) or {}
        rows = payload.get("rows", [])
        keys = ("challenger", "trading_date", "symbol") if family == "challenger" else (("trading_date", "symbol") if family == "momentum_acceleration_v2" else ("cohort", "trading_date", "symbol"))
        identities = [tuple(row.get(key) for key in keys) for row in rows]
        duplicates = sum(count - 1 for count in Counter(identities).values() if count > 1)
        mismatched_targets = 0
        versioned = 0
        for row in rows:
            for horizon in (1, 2, 3, 5, 7):
                block = row.get(f"d{horizon}") or {}
                if block.get("state") == "AVAILABLE":
                    versioned += int(bool(block.get("outcome_hash") and block.get("outcome_version")))
                    target = block.get("target_market_date")
                    spy = ((block.get("spy_provenance") or {}).get("close") or {}).get("market_date")
                    sector = ((block.get("sector_provenance") or {}).get("close") or {}).get("market_date")
                    if (spy and spy != target) or (sector and sector != target):
                        mismatched_targets += 1
        report["families"][family] = {"artifact": str(path), "rows": len(rows), "duplicate_memberships": duplicates, "benchmark_date_mismatches": mismatched_targets, "versioned_available_blocks": versioned}
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("backfill-versions", "coverage-report", "verify"))
    args = parser.parse_args()
    result = backfill_versions() if args.command == "backfill-versions" else coverage_report() if args.command == "coverage-report" else verify()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
