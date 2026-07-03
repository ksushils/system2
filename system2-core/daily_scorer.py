#!/usr/bin/env python3
"""
System 2 — Daily Scorer Wrapper.

Triggers the server's scoring endpoint, then logs any newly resolved
ideas to signal_outcomes.jsonl via nightly_learning.log_resolved_idea().

Usage:
  python3 daily_scorer.py

Cron (runs before nightly_learning):
  0 23 * * 1-5  cd /root/system2-core && .venv/bin/python daily_scorer.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from nightly_learning import log_resolved_idea, load_fund

API_URL = os.environ.get("FUND_API_URL", "http://127.0.0.1:3210")


def run() -> dict[str, any]:
    # 1. Trigger server scoring
    try:
        r = requests.post(f"{API_URL}/api/score/run", timeout=120)
        score_result = r.json() if r.ok else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        score_result = {"error": str(e)}

    # Small delay to let server write db
    time.sleep(2)

    # 2. Read fund and log newly resolved ideas
    fund = load_fund()
    ideas = fund.get("ideas", [])
    logged = 0
    already = 0
    for idea in ideas:
        if idea.get("paper_status") in ("CLOSED", "RESOLVED"):
            res = log_resolved_idea(idea)
            if res.get("already_logged"):
                already += 1
            elif res.get("ok"):
                logged += 1

    return {
        "ok": True,
        "scoring": score_result,
        "newly_logged": logged,
        "already_logged": already,
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
