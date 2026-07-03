#!/usr/bin/env python3
"""Unified idea lifecycle store for System 2.

Tracks every ticker's journey across the fast tier (continuous discovery) and
slow tier (nightly pipeline) in a single JSON file.

This is observability-only: failures here must never block the main flow.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LIFECYCLE_PATH = DATA_DIR / "idea_lifecycle.json"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_path() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not LIFECYCLE_PATH.exists():
        LIFECYCLE_PATH.write_text("[]", encoding="utf-8")


def load_lifecycle() -> list[dict[str, Any]]:
    """Load all lifecycle records."""
    _ensure_path()
    try:
        return json.loads(LIFECYCLE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_lifecycle(records: list[dict[str, Any]]) -> None:
    """Save lifecycle records."""
    _ensure_path()
    LIFECYCLE_PATH.write_text(json.dumps(records, indent=2), encoding="utf-8")


def _make_idea_id(ticker: str, first_seen_date: str) -> str:
    return f"{ticker.upper()}_{first_seen_date}"


def _journey_days(events: list[dict[str, Any]], from_stage: str, to_stage: str) -> int | None:
    """Return calendar days between first occurrence of two stages, or None."""
    from_ts = None
    to_ts = None
    for ev in events:
        if from_ts is None and ev.get("stage") == from_stage:
            from_ts = ev.get("timestamp", "")
        if ev.get("stage") == to_stage:
            to_ts = ev.get("timestamp", "")
    if not from_ts or not to_ts:
        return None
    try:
        d1 = datetime.fromisoformat(from_ts.replace("Z", "+00:00")).date()
        d2 = datetime.fromisoformat(to_ts.replace("Z", "+00:00")).date()
        return max(0, (d2 - d1).days)
    except Exception:
        return None


def record_stage(
    ticker: str,
    date: str | None,
    stage: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Record a lifecycle stage event for an idea.

    Args:
        ticker: The stock ticker.
        date: The calendar date for the idea instance (used in the idea_id).
            If None, today's date is used.
        stage: One of DISCOVERED, SCORED, FINALIST, ENTERED, RESOLVED.
        detail: Arbitrary detail dict to store with the event.

    Returns:
        The updated lifecycle record, or None on failure.
    """
    try:
        ticker = str(ticker or "").strip().upper()
        if not ticker:
            return None
        first_seen_date = str(date or _today()).strip()
        idea_id = _make_idea_id(ticker, first_seen_date)
        detail = dict(detail or {})
        detail["stage"] = stage
        detail["timestamp"] = datetime.now(timezone.utc).isoformat()

        records = load_lifecycle()
        record = next((r for r in records if r.get("idea_id") == idea_id), None)
        if record is None:
            record = {
                "idea_id": idea_id,
                "ticker": ticker,
                "first_seen_date": first_seen_date,
                "lifecycle": [],
                "current_stage": None,
                "origin": detail.get("source") or detail.get("origin"),
                "days_discovery_to_finalist": None,
                "days_finalist_to_entry": None,
                "final_outcome_r": None,
            }
            records.append(record)

        record["lifecycle"].append(detail)
        record["current_stage"] = stage

        # Recompute derived fields
        events = record["lifecycle"]
        if record["origin"] is None:
            record["origin"] = next(
                (ev.get("source") or ev.get("origin") for ev in events if ev.get("stage") == "DISCOVERED"),
                None,
            )
        record["days_discovery_to_finalist"] = _journey_days(events, "DISCOVERED", "FINALIST")
        record["days_finalist_to_entry"] = _journey_days(events, "FINALIST", "ENTERED")

        resolved = next(
            (ev for ev in reversed(events) if ev.get("stage") == "RESOLVED"),
            None,
        )
        if resolved:
            record["final_outcome_r"] = resolved.get("outcome_r")

        save_lifecycle(records)
        return record
    except Exception as exc:
        # Observability must never block the main flow.
        print(f"[idea_lifecycle] failed to record {stage} for {ticker}: {exc}")
        return None


def get_record(ticker: str, date: str | None = None) -> dict[str, Any] | None:
    """Get a single lifecycle record by ticker + date."""
    idea_id = _make_idea_id(ticker, date or _today())
    return next((r for r in load_lifecycle() if r.get("idea_id") == idea_id), None)


if __name__ == "__main__":
    # Quick sanity test
    record_stage("TEST", "2026-06-13", "DISCOVERED", {"source": "test", "detail": "manual test"})
    record_stage("TEST", "2026-06-13", "SCORED", {"setup_score": 72, "confluence_score": 65})
    print("Records:", len(load_lifecycle()))
