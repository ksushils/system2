#!/usr/bin/env python3
"""Signal 3 — Dark Pool Proxy for System 2 finalists.

Uses existing ohlcv_60 daily bars (already fetched for Stage 2).
No additional API calls. Ride-along only.
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
INPUT_PATH = ROOT / "stage2_surgical_strike_top40.json"
OUTPUT_PATH = ROOT / "stage2_dark_pool_enriched.json"
META_PATH = ROOT / "signal3_dark_pool_metadata.json"


def num(value, default=None) -> float | None:
    try:
        if value in (None, ""):
            return default
        v = float(value)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def analyze(bars: list[dict]) -> dict[str, Any]:
    """Compute dark-pool-proxy signals from daily OHLCV bars."""
    if not bars or len(bars) < 20:
        return {
            "dark_pool_proxy_score": None,
            "dark_pool_signal": "INSUFFICIENT_DATA",
            "close_to_close_avg_pct": None,
            "high_low_compression_pct": None,
            "volume_trend": None,
            "dark_pool_error": "need at least 20 bars",
        }
    # Ensure oldest-first
    sorted_bars = sorted(bars, key=lambda b: str(b.get("date") or ""))
    # Use last 20 days for primary metrics
    recent = sorted_bars[-20:]
    # Close-to-close daily changes
    changes = []
    for i in range(1, len(recent)):
        prev_close = num(recent[i - 1].get("close"))
        cur_close = num(recent[i].get("close"))
        if prev_close and cur_close and prev_close > 0:
            changes.append(abs((cur_close - prev_close) / prev_close) * 100)
    close_to_close_avg = round(statistics.mean(changes), 4) if changes else None
    # High-low compression
    hls = []
    for bar in recent:
        h = num(bar.get("high"))
        l = num(bar.get("low"))
        c = num(bar.get("close"))
        if h and l and c and c > 0:
            hls.append(((h - l) / c) * 100)
    high_low_compression = round(statistics.mean(hls), 4) if hls else None
    # Volume trend: 20d avg vs 40d avg
    vols = [num(b.get("volume"), 0) for b in sorted_bars if num(b.get("volume")) is not None]
    vol_20 = statistics.mean(vols[-20:]) if len(vols) >= 20 else None
    vol_40 = statistics.mean(vols[-40:]) if len(vols) >= 40 else None
    volume_trend = (
        "increasing" if vol_20 and vol_40 and vol_40 > 0 and (vol_20 - vol_40) / vol_40 >= 0.10
        else "flat"
    )
    # Score 0-3
    score = 0
    if close_to_close_avg is not None and close_to_close_avg < 0.5:
        score += 1
    if high_low_compression is not None and high_low_compression < 1.5:
        score += 1
    if volume_trend == "increasing":
        score += 1
    signal_map = {3: "STRONG", 2: "MODERATE", 1: "WEAK", 0: "NONE"}
    return {
        "dark_pool_proxy_score": score,
        "dark_pool_signal": signal_map.get(score, "NONE"),
        "close_to_close_avg_pct": close_to_close_avg,
        "high_low_compression_pct": high_low_compression,
        "volume_trend": volume_trend,
        "volume_20d_avg": round(vol_20, 2) if vol_20 else None,
        "volume_40d_avg": round(vol_40, 2) if vol_40 else None,
        "dark_pool_error": None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(INPUT_PATH))
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--metadata", default=str(META_PATH))
    args = parser.parse_args()

    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    enriched = []
    distribution: dict[str, int] = {}

    for row in rows:
        bars = row.get("ohlcv_60")
        result = analyze(bars if isinstance(bars, list) else [])
        signal = result["dark_pool_signal"]
        distribution[signal] = distribution.get(signal, 0) + 1
        enriched.append({**row, **result})

    metadata = {
        "stage": "SIGNAL3_DARK_POOL_PROXY",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "inputCount": len(rows),
        "outputCount": len(enriched),
        "signalDistribution": distribution,
        "mode": "ride_along_logging_only",
        "paperOnly": True,
    }

    Path(args.output).write_text(json.dumps(enriched, indent=2), encoding="utf-8")
    Path(args.metadata).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
