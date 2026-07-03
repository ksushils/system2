#!/usr/bin/env python3
"""
Phase D analyzer.

Reads the scoring loop stats after 30+ scored ideas and prints a plain-English
readout. It does not change rankings, gates, workflows, or trading behavior.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def fmt(value):
    return "n/a" if value is None else value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:3210")
    args = parser.parse_args()

    try:
        stats = get_json(f"{args.base}/api/score/stats")
    except urllib.error.HTTPError as exc:
        print(f"Could not read /api/score/stats: HTTP {exc.code}")
        print("If this endpoint is admin-protected, run from an authenticated context or add the required header.")
        raise SystemExit(1)

    scored = stats.get("primary_r_count") or stats.get("scored_3d") or 0
    print(json.dumps(stats, indent=2))
    print("\nPHASE D READOUT")
    print(f"- Ideas with primary R: {scored}")
    print("- Primary R rule: actual_r where available; planned_r only for legacy records.")
    if scored < 30:
        print("- Decision: wait. Need at least 30 scored ideas before changing any layer.")
        return

    print(f"- Primary-R win rate: {fmt(stats.get('win_rate_primary', stats.get('win_rate_3d')))}%")
    print(f"- Avg primary R: {fmt(stats.get('avg_primary_r', stats.get('avg_r_3d')))}")
    print(f"- Avg entry gap: {fmt(stats.get('avg_entry_gap_pct'))}%")

    options = stats.get("options_compare") or {}
    if options:
        print(
            "- Options confirmed vs not: "
            f"{fmt(options.get('confirm_avg_r'))} R "
            f"({fmt(options.get('confirm_count'))} ideas) vs "
            f"{fmt(options.get('non_confirm_avg_r'))} R "
            f"({fmt(options.get('non_confirm_count'))} ideas)"
        )

    chronos = stats.get("chronos_band_compare") or {}
    if chronos:
        print(
            "- Chronos tight vs wide: "
            f"{fmt(chronos.get('tight_band_avg_r'))} R vs "
            f"{fmt(chronos.get('wide_band_avg_r'))} R"
        )

    source = stats.get("source_compare") or {}
    if source:
        print("- Source cohorts:")
        for name, row in source.items():
            print(
                f"  {name}: count={row.get('count')} "
                f"avg_primary_r={fmt(row.get('avg_primary_r', row.get('avg_r_3d')))}"
            )

    print("- Decision rule: promote only one layer per measurement window if its cohort beats baseline.")


if __name__ == "__main__":
    main()
