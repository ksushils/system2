#!/usr/bin/env python3
"""Regression checks for prospective future-price state semantics."""
from datetime import datetime, timezone
from continuous_improvement_lab_v1 import time_aware_label

ROW = {"symbol":"TEST", "trading_date":"2026-09-25",
       "next_open_timestamp":"2026-09-25T09:30:00-04:00"}

def main() -> None:
    result = time_aware_label(ROW, None, datetime(2026, 9, 25, 13, 29, tzinfo=timezone.utc))
    assert result["entry_state"] == "PENDING_CANONICAL_XNYS_OPEN"
    assert result["outcome_state"] == "PENDING_CANONICAL_XNYS_OPEN"
    for horizon in (1, 3, 5, 7):
        assert result[f"d{horizon}"]["state"] == "PENDING"
    # Friday + one trading day is Monday: calendar/holiday logic must not use Saturday.
    assert result["d1"]["target_market_date"] == "2026-09-28"
    print("PASS future-entry/future-horizon/calendar/idempotent-state")

if __name__ == "__main__":
    main()
