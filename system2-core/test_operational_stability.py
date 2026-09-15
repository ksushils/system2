#!/usr/bin/env python3
"""Pure regression fixtures for operational stability repairs."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from entry_trigger_monitor import partition_monitor_ideas
from intelligence_engine import cached_shadow_5d_return


def main() -> None:
    rows = [
        {"ticker": "WATCH", "entry": 10, "paper_status": "WATCHING"},
        {"ticker": "OPEN", "entry": 20, "paper_status": "OPEN", "paper_entry_price": 20},
        {"ticker": "CLOSED", "entry": 30, "paper_status": "CLOSED"},
    ]
    for _ in range(1000):
        watch, opened = partition_monitor_ideas(rows)
        assert [r["ticker"] for r in watch] == ["WATCH"]
        assert [r["ticker"] for r in opened] == ["OPEN"]
    assert cached_shadow_5d_return(1.25) == 1.25
    assert cached_shadow_5d_return({"shadow_5d_return": -0.75}) == -0.75
    assert cached_shadow_5d_return({"shadow_10d_return": 2}) is None
    print("PASS operational stability fixtures")


if __name__ == "__main__":
    main()
