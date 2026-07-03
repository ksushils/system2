#!/usr/bin/env python3
import json
import tempfile
import os
from pathlib import Path

# Import from the VPS copy
import sys
sys.path.insert(0, '/root/system2-core')
from nightly_learning import (
    compute_council_calibration,
    generate_council_suggestions,
    SIGNAL_OUTCOMES_PATH,
    COUNCIL_CALIBRATION_PATH,
    COUNCIL_SUGGESTIONS_PATH,
    append_jsonl,
)

# Test 1: empty data
print("TEST 1 — Empty data:")
cal = compute_council_calibration()
print(f"  insufficient_data: {cal.get('insufficient_data')}")
print(f"  count: {cal.get('count')}")
print(f"  needed: {cal.get('needed')}")

# Test 2: suggestion generator with empty data
sug = generate_council_suggestions(cal)
print(f"\nTEST 2 — Suggestions from empty data:")
print(f"  suggestions count: {len(sug)}")
print(f"  is list: {isinstance(sug, list)}")

# Test 3: mock data with sufficient samples
mock_records = []
for i in range(10):
    mock_records.append({
        "id": f"mock_{i}",
        "actual_r": 0.5 + i * 0.1,
        "gemini_verdict": "TIER1" if i < 5 else "SKIP",
        "claude_verdict": "UPGRADE" if i < 4 else "FORCE_SKIP",
        "kimi_verdict": "ABSTAIN" if i % 2 == 0 else "TIER2",
        "gpt4o_verdict": "TIER1",
        "council_final_verdict": "TIER1",
    })

with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
    for r in mock_records:
        f.write(json.dumps(r) + "\n")
    tmp_path = f.name

import nightly_learning
orig_path = nightly_learning.SIGNAL_OUTCOMES_PATH
nightly_learning.SIGNAL_OUTCOMES_PATH = Path(tmp_path)

cal2 = compute_council_calibration()
print(f"\nTEST 3 — Mock data (10 resolved):")
print(f"  insufficient_data: {cal2.get('insufficient_data')}")
print(f"  total_resolved: {cal2.get('total_resolved')}")
for mk, ms in (cal2.get('models') or {}).items():
    print(f"  {mk}: total={ms.get('total_verdicts')}, tier1_avg={ms.get('tier1_avg_r')}, skip_avg={ms.get('skip_avg_r')}, calibrated={ms.get('calibrated')}, force_skip={ms.get('force_skip_rate')}")

sug2 = generate_council_suggestions(cal2)
print(f"\nTEST 4 — Suggestions from mock data:")
print(f"  suggestions count: {len(sug2)}")
for s in sug2:
    print(f"  - {s['model']}: {s['issue']} — {s['finding']}")

os.unlink(tmp_path)
nightly_learning.SIGNAL_OUTCOMES_PATH = orig_path

# Test 5: verify JSON artifacts
print(f"\nTEST 5 — JSON artifacts:")
print(f"  calibration path: {COUNCIL_CALIBRATION_PATH}")
print(f"  suggestions path: {COUNCIL_SUGGESTIONS_PATH}")
