import json
import math


ACCOUNT_RISK = 250.0


with open("/root/system2-core/june4_investigation_after_recalc.json") as f:
    investigation = json.load(f)

rows = []
for trade in investigation["trades"]:
    entry = trade["entry"]
    old_stop = trade["stop"]
    atr = trade["atr14_calc"]
    old_distance = entry - old_stop
    preferred_distance = 1.25 * atr
    max_width_distance = 0.04 * entry
    # ATR floor takes priority when ATR itself is wider than the 4% cap.
    new_distance = max(atr, min(preferred_distance, max_width_distance))
    new_stop = entry - new_distance
    new_tp1 = entry + 2 * new_distance
    new_tp2 = entry + 3 * new_distance
    shares = math.floor(ACCOUNT_RISK / new_distance) if new_distance > 0 else 0
    jun5_low = trade["jun5_low"]
    jun5_open = trade["jun5_open"]
    stopped = jun5_low is not None and jun5_low <= new_stop
    rows.append({
        "ticker": trade["ticker"],
        "entry": round(entry, 4),
        "old_stop": round(old_stop, 4),
        "old_stop_atr": round(old_distance / atr, 2),
        "atr": atr,
        "atr_pct": round(atr / entry * 100, 2),
        "new_stop": round(new_stop, 4),
        "new_stop_atr": round(new_distance / atr, 2),
        "new_stop_pct": round(new_distance / entry * 100, 2),
        "new_tp1": round(new_tp1, 4),
        "new_tp2": round(new_tp2, 4),
        "new_rr": 2.0,
        "new_shares": shares,
        "new_dollar_risk": round(shares * new_distance, 2),
        "jun5_open": jun5_open,
        "jun5_low": jun5_low,
        "stopped_jun5": stopped,
        "gap_through": jun5_open is not None and jun5_open < new_stop,
        "would_survive_jun5": not stopped,
        "cap_conflict": atr > max_width_distance,
    })

print(json.dumps(rows, indent=2))
