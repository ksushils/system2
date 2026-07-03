#!/usr/bin/env python3
"""Discovery feedback loop for System 2.

Analyses the unified idea lifecycle records to compute how effectively the
continuous discovery tier predicts finalists and winners. This feedback
tunes the alert thresholds in continuous_discovery.py.

Gating: feedback tuning only activates once total_discovered >= 30.
Below that the file is still written, but it recommends default thresholds.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from idea_lifecycle import load_lifecycle


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
FEEDBACK_PATH = DATA_DIR / "discovery_feedback.json"
CONTINUOUS_FEED_PATH = DATA_DIR / "continuous_discovery_feed.json"


def _today() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save(data: Any) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FEEDBACK_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _pct(part: int, whole: int) -> float | None:
    if whole == 0:
        return None
    return round(part / whole * 100, 1)


def _signal_source(event: dict[str, Any]) -> str | None:
    """Extract the first discovery source from a DISCOVERED event."""
    sources = event.get("sources_agreeing") or []
    if sources:
        return sources[0]
    return event.get("source")


def build_feedback() -> dict[str, Any]:
    lifecycle = load_lifecycle()

    # Index lifecycle records by ticker+first_seen_date for fast lookup.
    by_idea: dict[str, dict[str, Any]] = {r["idea_id"]: r for r in lifecycle if "idea_id" in r}

    # All DISCOVERED events.
    discovered_events: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for rec in lifecycle:
        for ev in rec.get("lifecycle", []):
            if ev.get("stage") == "DISCOVERED":
                discovered_events.append((rec["idea_id"], rec, ev))

    total_discovered = len(discovered_events)

    # Group discovered events by source.
    source_stats: dict[str, dict[str, Any]] = {}
    for idea_id, _rec, ev in discovered_events:
        primary_source = _signal_source(ev) or "unknown"
        if primary_source not in source_stats:
            source_stats[primary_source] = {
                "discovered": 0,
                "became_finalist": 0,
                "resolved": 0,
                "wins": 0,
            }
        source_stats[primary_source]["discovered"] += 1

    # For each discovered idea, determine if it became a finalist and whether it won.
    became_finalist = 0
    finalist_to_win = 0
    finalist_to_resolve = 0

    for idea_id, rec, ev in discovered_events:
        stages = [x.get("stage") for x in rec.get("lifecycle", [])]
        if "FINALIST" in stages:
            became_finalist += 1
            primary_source = _signal_source(ev) or "unknown"
            source_stats[primary_source]["became_finalist"] += 1

            # Resolved outcome (from RESOLVED event or final_outcome_r)
            resolved = next(
                (x for x in reversed(rec.get("lifecycle", [])) if x.get("stage") == "RESOLVED"),
                None,
            )
            outcome_r = resolved.get("outcome_r") if resolved else rec.get("final_outcome_r")
            if outcome_r is not None:
                finalist_to_resolve += 1
                source_stats[primary_source]["resolved"] += 1
                if float(outcome_r) > 0:
                    finalist_to_win += 1
                    source_stats[primary_source]["wins"] += 1

    # Compute conversion rates per source.
    by_source: dict[str, dict[str, Any]] = {}
    for src, s in source_stats.items():
        by_source[src] = {
            "discovered": s["discovered"],
            "became_finalist": s["became_finalist"],
            "conversion_pct": _pct(s["became_finalist"], s["discovered"]),
            "resolved": s["resolved"],
            "win_rate": _pct(s["wins"], s["resolved"]),
        }

    # Recommendations.
    trust_sources = []
    raise_bar_sources = []
    if total_discovered >= 30:
        for src, s in by_source.items():
            conv = s["conversion_pct"] or 0
            if conv >= 40:
                trust_sources.append(src)
            elif conv < 10:
                raise_bar_sources.append(src)

    overall = {
        "discovered_to_finalist_pct": _pct(became_finalist, total_discovered),
        "finalist_win_rate": _pct(finalist_to_win, finalist_to_resolve),
    }

    recommendation = {
        "raise_bar_sources": raise_bar_sources,
        "trust_sources": trust_sources,
        "message": (
            "Feedback tuning active" if total_discovered >= 30
            else f"Feedback dormant — {total_discovered}/30 discovered. Need more data before tuning thresholds."
        ),
    }

    best_source = None
    best_conv = -1.0
    for src, s in by_source.items():
        conv = s["conversion_pct"] or 0
        if conv > best_conv:
            best_conv = conv
            best_source = src

    result = {
        "computed_at": _today(),
        "total_discovered": total_discovered,
        "discovered_to_finalist_pct": overall["discovered_to_finalist_pct"],
        "finalist_win_rate": overall["finalist_win_rate"],
        "by_source": by_source,
        "recommendation": recommendation,
        "best_source": best_source,
        "best_conversion_pct": best_conv if best_conv >= 0 else None,
        "tuning_active": total_discovered >= 30,
    }

    _save(result)
    return result


def main() -> None:
    result = build_feedback()
    print(json.dumps({
        "total_discovered": result["total_discovered"],
        "discovered_to_finalist_pct": result["discovered_to_finalist_pct"],
        "finalist_win_rate": result["finalist_win_rate"],
        "tuning_active": result["tuning_active"],
        "best_source": result["best_source"],
    }, indent=2))


if __name__ == "__main__":
    main()
