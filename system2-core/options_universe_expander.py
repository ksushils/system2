#!/usr/bin/env python3
"""Options Universe Expander for System 2.

Reads free options-flow sources (Barchart UOA, ApeWisdom spikes) and adds
high-conviction tickers to tonight's universe even if they are not in the
base 1500. The base universe builder then merges these additions before
writing universe.json.

Thresholds:
  - Barchart UOA: vol/OI >= 10.0
  - ApeWisdom: rank_improvement > 100 and mentions > 10
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "data" / "options_universe_expansion.json"
UNIVERSE_PATH = ROOT / "universe.json"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_universe_tickers() -> set[str]:
    data = load_json(UNIVERSE_PATH)
    if isinstance(data, list):
        return {str(t).upper().strip() for t in data if t}
    if isinstance(data, dict) and "tickers" in data:
        return {str(t).upper().strip() for t in data["tickers"] if t}
    return set()


def expand_universe_from_options() -> dict[str, Any]:
    """Build the options-flow expansion list."""
    started = datetime.now(timezone.utc)
    base_tickers = load_universe_tickers()
    additions: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Load Barchart UOA
    bc_data = load_json(ROOT / "data" / "barchart_uoa.json")
    for c in bc_data.get("candidates", []):
        try:
            vol_oi = float(c.get("vol_oi_ratio", 0))
        except Exception:
            vol_oi = 0.0
        ticker = str(c.get("symbol", "")).upper().strip()
        if not ticker or ticker in seen or ticker in base_tickers:
            continue
        if vol_oi >= 10.0:
            additions.append({
                "ticker": ticker,
                "source": "barchart_uoa_expansion",
                "vol_oi_ratio": vol_oi,
                "added_reason": f"vol/OI {vol_oi:.1f}x",
            })
            seen.add(ticker)

    # Load ApeWisdom spikes
    ape_data = load_json(ROOT / "data" / "apewisdom_sentiment.json")
    for ticker, info in (ape_data.get("stocks") or {}).items():
        ticker = str(ticker).upper().strip()
        if not ticker or ticker in seen or ticker in base_tickers:
            continue
        rank_improvement = info.get("rank_improvement", 0) or 0
        mentions = info.get("mentions", 0) or 0
        if rank_improvement > 100 and mentions > 10:
            additions.append({
                "ticker": ticker,
                "source": "apewisdom_expansion",
                "rank_improvement": rank_improvement,
                "mentions": mentions,
                "added_reason": (
                    f"ApeWisdom rank +{rank_improvement} with {mentions} mentions"
                ),
            })
            seen.add(ticker)

    output = {
        "date": started.date().isoformat(),
        "generated_at": started.isoformat(),
        "source": "options_universe_expansion",
        "base_universe_size": len(base_tickers),
        "added_count": len(additions),
        "additions": additions,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(
        f"Options universe expansion: {len(additions)} tickers added "
        f"to {OUTPUT_PATH}"
    )
    return output


def main() -> None:
    expand_universe_from_options()


if __name__ == "__main__":
    main()
