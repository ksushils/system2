#!/usr/bin/env python3
"""
System 2 smoke checks.

Read-only except for an optional /api/idea test when --post-test-idea is used.
No broker calls, no live trading.
"""

from __future__ import annotations

import argparse
import json
import urllib.request


def get_json(url: str, timeout: int = 20):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def post_json(url: str, payload: dict, timeout: int = 60):
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fund-base", default="http://127.0.0.1:3210")
    parser.add_argument("--options-base", default="http://127.0.0.1:8002")
    parser.add_argument("--post-test-idea", action="store_true")
    args = parser.parse_args()

    out = {}
    out["options_healthz"] = get_json(f"{args.options_base}/healthz")
    out["options_flow_sample"] = post_json(
        f"{args.options_base}/options-flow",
        {
            "tickers": ["AAPL", "NVDA", "PNC"],
            "prices": {"AAPL": 311.23, "NVDA": 188.89, "PNC": 227.11},
        },
        timeout=180,
    )

    if args.post_test_idea:
        out["idea_post"] = post_json(
            f"{args.fund_base}/api/idea",
            {
                "ticker": "TESTSYS2",
                "entry": 100,
                "stop": 98,
                "target": 106,
                "paper": True,
                "source": "scanner",
                "options_verdict": "NEUTRAL",
                "options_signals_count": 1,
            },
        )

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
