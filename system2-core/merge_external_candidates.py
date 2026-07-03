#!/usr/bin/env python3
"""
Merge external candidate sources into candidate_pool.json.
Runs after x_candidate_extractor.py --merge-only and before B2 Stage 1.
"""
import json
import os
from pathlib import Path

ROOT = Path("/root/system2-core")
DATA_DIR = ROOT / "data"


def load_json(path: Path, default=None):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def main():
    pool_path = ROOT / "candidate_pool.json"
    pool = load_json(pool_path, [])
    if not isinstance(pool, list):
        print("candidate_pool.json is not a list, skipping merge")
        return

    existing_tickers = {str(row.get("ticker") or row.get("symbol", "")).upper() for row in pool}
    added = 0

    # Merge Barchart UOA candidates
    barchart = load_json(DATA_DIR / "barchart_uoa.json", {})
    for cand in barchart.get("candidates", []):
        sym = str(cand.get("symbol", "")).upper()
        if not sym or sym in existing_tickers:
            continue
        pool.append({
            "ticker": sym,
            "symbol": sym,
            "source": "barchart_uoa",
            "sub_type": "unusual_call_activity",
            "sub_types": ["unusual_call_activity"],
            "catalyst_summary": f"Barchart unusual call volume detected: vol/OI ratio {cand.get('vol_oi_ratio', '-')}x",
            "catalyst_date": barchart.get("date"),
            "catalyst_score": 1.0,
            "catalyst_sources": ["barchart_uoa"],
            "price": cand.get("underlying_price"),
            "option_type": cand.get("option_type"),
            "strike": cand.get("strike"),
            "expiration": cand.get("expiration"),
        })
        existing_tickers.add(sym)
        added += 1

    # Merge StockAnalysis high-short candidates
    high_short = load_json(DATA_DIR / "high_short_universe.json", {})
    for stock in high_short.get("stocks", []):
        sym = str(stock.get("symbol", "")).upper()
        if not sym or sym in existing_tickers:
            continue
        short_pct = stock.get("short_percent_float", 0)
        if short_pct <= 0.15:
            continue
        pool.append({
            "ticker": sym,
            "symbol": sym,
            "source": "stockanalysis_short_squeeze",
            "sub_type": "short_squeeze",
            "sub_types": ["short_squeeze"],
            "catalyst_summary": f"High short interest candidate: {short_pct*100:.1f}% of float short",
            "catalyst_date": high_short.get("scraped_at", "").split("T")[0] if high_short.get("scraped_at") else None,
            "catalyst_score": 1.0,
            "catalyst_sources": ["stockanalysis"],
            "short_percent_float": short_pct,
            "market_cap": stock.get("market_cap"),
        })
        existing_tickers.add(sym)
        added += 1

    with open(pool_path, "w", encoding="utf-8") as f:
        json.dump(pool, f, indent=2)

    print(f"Merged {added} external candidates into candidate_pool.json (total: {len(pool)})")
    print(json.dumps({"ok": True, "added": added, "total": len(pool)}))


if __name__ == "__main__":
    main()
