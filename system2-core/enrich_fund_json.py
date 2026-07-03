#!/usr/bin/env python3
"""Retroactively enrich all ideas in fund.json with Prompt 2 fields."""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

FUND_PATH = Path("/root/fund-system/data/fund.json")
LEGACY_CUTOFF = datetime(2026, 6, 9, 2, 15, 0, tzinfo=timezone.utc)
SLIPPAGE_PCT_ROUND_TRIP = 0.003


def num(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        v = float(value)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _get_or_none(i, key):
    v = i.get(key)
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def enrich_idea(i: dict) -> dict:
    # ERA
    logged_at = i.get("logged_at") or i.get("date")
    try:
        dt = datetime.fromisoformat(str(logged_at).replace("Z", "+00:00"))
        i["era"] = "legacy" if dt < LEGACY_CUTOFF else "system2_v2"
    except Exception:
        i["era"] = "legacy"

    # IDEA ENTRY PRICE
    if i.get("idea_entry_price") is None:
        pre_market = _get_or_none(i, "pre_market_price")
        zone = i.get("entryZone")
        if isinstance(zone, list) and len(zone) >= 2:
            zone_mid = (num(zone[0]) + num(zone[1])) / 2
            i["idea_entry_price"] = round(zone_mid, 4)
            i["idea_tracking_source"] = "zone_midpoint"
        elif pre_market and pre_market > 0:
            i["idea_entry_price"] = round(pre_market, 4)
            i["idea_tracking_source"] = "pre_market"
        elif i.get("entry") and num(i.get("entry")) > 0:
            i["idea_entry_price"] = round(num(i.get("entry")), 4)
            i["idea_tracking_source"] = "fmp_scan"

    # IDEA PERFORMANCE
    entry = _get_or_none(i, "idea_entry_price")
    stop = _get_or_none(i, "stop")
    target = _get_or_none(i, "target")

    if i.get("idea_r") is None:
        proxy = _get_or_none(i, "r_3d") or _get_or_none(i, "r_10d") or _get_or_none(i, "paper_exit_r")
        if proxy is not None:
            i["idea_r"] = round(proxy, 3)
            if i.get("hit") in ("TARGET", "TP1") or proxy > 0:
                i["idea_outcome"] = "winner"
            elif i.get("hit") in ("STOP",) or proxy < 0:
                i["idea_outcome"] = "loser"
            elif i.get("hit") == "TIME":
                i["idea_outcome"] = "timeout"
            else:
                i["idea_outcome"] = "unentered"

    # TRADE ENTERED
    if i.get("trade_entered") is None:
        i["trade_entered"] = (
            i.get("actual_entry_price") is not None or
            i.get("paper_exit_r") is not None or
            i.get("actual_r") is not None
        )

    # SLIPPAGE
    if i.get("slippage_pct") is None:
        i["slippage_pct"] = 0.15

    if i.get("slippage_r") is None:
        atr14 = _get_or_none(i, "atr14")
        entry_price = _get_or_none(i, "actual_entry_price") or entry
        if entry_price and entry_price > 0 and atr14 and atr14 > 0:
            i["slippage_r"] = round((entry_price * SLIPPAGE_PCT_ROUND_TRIP) / atr14, 3)

    # trade_r_gross / trade_r_net
    actual_r = _get_or_none(i, "actual_r")
    paper_exit_r = _get_or_none(i, "paper_exit_r")
    gross = actual_r if actual_r is not None else paper_exit_r
    if gross is not None:
        if i.get("trade_r_gross") is None:
            i["trade_r_gross"] = round(gross, 3)
        if i.get("trade_r_net") is None:
            slip = num(i.get("slippage_r"), 0)
            i["trade_r_net"] = round(gross - slip, 3)

    # MFE/MAE in R
    stop_dist_pct = None
    if entry and stop and entry > 0 and stop > 0:
        stop_dist_pct = abs((entry - stop) / entry * 100)
    elif i.get("distanceFromVWAP"):
        stop_dist_pct = abs(num(i.get("distanceFromVWAP")))

    mfe_pct = _get_or_none(i, "mfe_pct")
    mae_pct = _get_or_none(i, "mae_pct")

    if mfe_pct is not None and stop_dist_pct and stop_dist_pct > 0 and i.get("mfe_r") is None:
        i["mfe_r"] = round(mfe_pct / stop_dist_pct, 3)
    if mae_pct is not None and stop_dist_pct and stop_dist_pct > 0 and i.get("mae_r") is None:
        i["mae_r"] = round(mae_pct / stop_dist_pct, 3)

    # capture_rate
    if i.get("capture_rate") is None:
        mfe = _get_or_none(i, "mfe_r")
        gross = _get_or_none(i, "trade_r_gross")
        if mfe and mfe > 0 and gross is not None:
            i["capture_rate"] = round(gross / mfe, 3)

    return i


def main():
    data = json.loads(FUND_PATH.read_text(encoding="utf-8"))
    ideas = data.get("ideas", [])
    enriched = [enrich_idea(i) for i in ideas]
    data["ideas"] = enriched
    FUND_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    v2 = [i for i in enriched if i.get("era") == "system2_v2"]
    legacy = [i for i in enriched if i.get("era") == "legacy"]
    with_idea_r = [i for i in enriched if i.get("idea_r") is not None]
    with_trade_r = [i for i in enriched if i.get("trade_r_net") is not None]
    print(f"Total ideas: {len(enriched)}")
    print(f"  v2: {len(v2)}")
    print(f"  legacy: {len(legacy)}")
    print(f"  With idea_r: {len(with_idea_r)}")
    print(f"  With trade_r_net: {len(with_trade_r)}")


if __name__ == "__main__":
    main()
