#!/usr/bin/env bash
# Set 3 Catalyst Universe — Preview / Staged Run
# NOT wired into production pipeline. Run manually for testing.
# Requires: catalyst_discovery.py, set3_scorer.py, merge_sets.py

set -euo pipefail

cd /root/system2-core

echo "=== Set 3 Preview Run ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Step 1: Run catalyst discovery (uses FMP stable endpoints)
echo ""
echo "[1/3] Catalyst Discovery..."
python3 catalyst_discovery.py --limit 50 --lookback-hours 72

# Step 2: Score catalyst candidates
echo ""
echo "[2/3] Set 3 Scoring..."
python3 set3_scorer.py

# Step 3: Preview merge (writes to data/merged_sets_preview.json, NOT to Set 1 path)
echo ""
echo "[3/3] Preview Merge (dry-run, does not overwrite Set 1)..."
python3 << 'PYEOF'
import json
from pathlib import Path

root = Path("/root/system2-core")
set1_path = root / "stage2_confluence_ranked_top40.json"
set3_path = root / "data" / "set3_scored.json"

set1 = json.loads(set1_path.read_text(encoding="utf-8-sig"))
if isinstance(set1, dict):
    set1 = set1.get("ideas") or set1.get("candidates") or []

set3_data = json.loads(set3_path.read_text(encoding="utf-8"))
set3 = set3_data.get("candidates") or []

set1_syms = {str(r.get("symbol") or r.get("ticker") or "").upper().strip() for r in set1}
set3_syms = {str(r.get("symbol") or r.get("ticker") or "").upper().strip() for r in set3}

overlap = set1_syms & set3_syms
print(f"Set 1: {len(set1)} ideas")
print(f"Set 3: {len(set3)} ideas")
print(f"Overlap: {len(overlap)} symbols")
if overlap:
    print(f"  Overlapping: {sorted(overlap)}")

# Save preview merge log
preview = {
    "preview_date": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    "set1_count": len(set1),
    "set3_count": len(set3),
    "overlap_count": len(overlap),
    "overlap_symbols": sorted(overlap),
    "set3_candidates": set3,
}
preview_path = root / "data" / "set3_preview_merge.json"
preview_path.write_text(json.dumps(preview, indent=2, default=str), encoding="utf-8")
print(f"Preview saved to {preview_path}")
PYEOF

echo ""
echo "=== Set 3 Preview Complete ==="
echo "Files:"
echo "  - catalyst_candidates.json"
echo "  - data/set3_scored.json"
echo "  - data/set3_scored_metadata.json"
echo "  - data/set3_preview_merge.json"
echo ""
echo "NOTE: Set 3 is STAGED. Do not wire into pipeline until Set 2 reaches 5+ resolved ideas."
