#!/usr/bin/env python3
"""
System 2 — Options Structure Engine.

Computes per-ticker:
  • Max Pain (weekly + monthly expiries)
  • Call Walls / Put Walls
  • Gamma Flip proxy
  • TP wall conflict / Stop wall support checks

Uses raw options chain contracts (from Yahoo provider or similar).
Each contract must have:
  strike: float
  option_type: "CALL" | "PUT"
  expiry: "YYYY-MM-DD"
  DTE: int
  open_interest: float | None
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def bucket_by_expiry(contracts: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for c in contracts:
        exp = c.get("expiry")
        if exp:
            buckets[exp].append(c)
    return dict(buckets)


def sort_expiry_by_dte(buckets: dict[str, list[dict]]) -> list[str]:
    def dte(exp: str) -> int:
        cs = buckets.get(exp, [])
        if cs:
            return min(c.get("DTE", 999) for c in cs)
        return 999
    return sorted(buckets.keys(), key=dte)


# ═══════════════════════════════════════════════════════════════════════════════
# MAX PAIN
# ═══════════════════════════════════════════════════════════════════════════════

def compute_max_pain_for_expiry(contracts: list[dict]) -> dict[str, Any] | None:
    """
    For a single expiry bucket, compute max pain strike.
    Returns dict with strike, total_pain, days_to_expiry or None if insufficient data.
    """
    # Build OI maps per strike
    call_oi: dict[float, float] = defaultdict(float)
    put_oi: dict[float, float] = defaultdict(float)
    all_strikes: set[float] = set()

    for c in contracts:
        strike = num(c.get("strike"))
        oi = num(c.get("open_interest"))
        opt_type = str(c.get("option_type", "")).upper()
        if strike is None or oi is None or oi <= 0:
            continue
        all_strikes.add(strike)
        if opt_type == "CALL":
            call_oi[strike] += oi
        elif opt_type == "PUT":
            put_oi[strike] += oi

    if len(all_strikes) < 2:
        return None

    strikes = sorted(all_strikes)
    dte = min(c.get("DTE", 999) for c in contracts if c.get("DTE") is not None) or 0

    # For each candidate strike S, compute total pain
    min_pain = float("inf")
    max_pain_strike = strikes[0]
    for S in strikes:
        call_pain = 0.0
        put_pain = 0.0
        for K, oi in call_oi.items():
            if K < S:
                call_pain += (S - K) * oi * 100
        for K, oi in put_oi.items():
            if K > S:
                put_pain += (K - S) * oi * 100
        total_pain = call_pain + put_pain
        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = S

    return {
        "strike": max_pain_strike,
        "total_pain": round(min_pain, 2),
        "days_to_expiry": dte,
    }


def classify_max_pain(price: float, max_pain: float | None) -> str:
    if price <= 0 or max_pain is None or max_pain <= 0:
        return "UNKNOWN"
    pct = (price - max_pain) / max_pain * 100
    if pct > 3:
        return "ABOVE_PAIN"
    if pct < -3:
        return "BELOW_PAIN"
    return "AT_PAIN"


# ═══════════════════════════════════════════════════════════════════════════════
# OPTIONS WALLS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_walls(contracts: list[dict], price: float) -> dict[str, Any]:
    """
    Find call walls (highest call OI above price) and put walls (highest put OI below price).
    Returns all None if total OI across all contracts is 0 (prevents garbage walls on Sunday).
    """
    call_oi_by_strike: dict[float, float] = defaultdict(float)
    put_oi_by_strike: dict[float, float] = defaultdict(float)
    total_oi = 0.0

    for c in contracts:
        strike = num(c.get("strike"))
        oi = num(c.get("open_interest"))
        opt_type = str(c.get("option_type", "")).upper()
        if strike is None or oi is None:
            continue
        total_oi += oi
        if opt_type == "CALL":
            call_oi_by_strike[strike] += oi
        elif opt_type == "PUT":
            put_oi_by_strike[strike] += oi

    # Guard: if total OI is 0, return all None (weekend / no data)
    if total_oi <= 0:
        return {
            "call_wall_1": None, "call_wall_1_oi": None,
            "call_wall_2": None, "call_wall_2_oi": None,
            "put_wall_1": None, "put_wall_1_oi": None,
            "put_wall_2": None, "put_wall_2_oi": None,
        }

    # Call walls: strikes > price, sorted by OI desc
    call_walls = sorted(
        [(s, oi) for s, oi in call_oi_by_strike.items() if s > price],
        key=lambda x: x[1],
        reverse=True,
    )
    # Put walls: strikes < price, sorted by OI desc
    put_walls = sorted(
        [(s, oi) for s, oi in put_oi_by_strike.items() if s < price],
        key=lambda x: x[1],
        reverse=True,
    )

    return {
        "call_wall_1": call_walls[0][0] if len(call_walls) >= 1 else None,
        "call_wall_1_oi": int(call_walls[0][1]) if len(call_walls) >= 1 else None,
        "call_wall_2": call_walls[1][0] if len(call_walls) >= 2 else None,
        "call_wall_2_oi": int(call_walls[1][1]) if len(call_walls) >= 2 else None,
        "put_wall_1": put_walls[0][0] if len(put_walls) >= 1 else None,
        "put_wall_1_oi": int(put_walls[0][1]) if len(put_walls) >= 1 else None,
        "put_wall_2": put_walls[1][0] if len(put_walls) >= 2 else None,
        "put_wall_2_oi": int(put_walls[1][1]) if len(put_walls) >= 2 else None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GAMMA FLIP PROXY
# ═══════════════════════════════════════════════════════════════════════════════

def compute_gamma_flip_proxy(contracts: list[dict]) -> dict[str, Any] | None:
    """
    Approximate gamma flip = strike where net OI (call OI - put OI) crosses zero.
    Returns the crossover strike and direction classification.
    """
    net_oi: dict[float, float] = defaultdict(float)
    total_oi = 0.0

    for c in contracts:
        strike = num(c.get("strike"))
        oi = num(c.get("open_interest"))
        opt_type = str(c.get("option_type", "")).upper()
        if strike is None or oi is None:
            continue
        total_oi += oi
        if opt_type == "CALL":
            net_oi[strike] += oi
        elif opt_type == "PUT":
            net_oi[strike] -= oi

    # Guard: if total OI is 0, return None (weekend / no data)
    if total_oi <= 0 or not net_oi:
        return None

    strikes = sorted(net_oi.keys())
    # Scan from lowest to highest strike, find where net_oi crosses from negative to positive
    prev_strike = None
    prev_net = None
    for s in strikes:
        net = net_oi[s]
        if prev_net is not None and prev_net < 0 and net >= 0:
            # Crossover between prev_strike and s
            # Linear interpolation
            if prev_net == net:
                flip = s
            else:
                frac = abs(prev_net) / (abs(prev_net) + net)
                flip = prev_strike + frac * (s - prev_strike) if prev_strike else s
            return {"strike": round(flip, 2), "crossover_strikes": [prev_strike, s]}
        prev_strike = s
        prev_net = net

    # No crossover found — all negative or all positive
    # Return the strike closest to zero net OI
    closest_strike = min(strikes, key=lambda s: abs(net_oi[s]))
    return {"strike": closest_strike, "crossover_strikes": None}


def classify_gamma_flip(price: float, flip: float | None) -> str:
    if price <= 0 or flip is None or flip <= 0:
        return "UNKNOWN"
    return "ABOVE_FLIP" if price > flip else "BELOW_FLIP"


# ═══════════════════════════════════════════════════════════════════════════════
# TP / STOP WALL CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_tp_wall_check(
    tp1: float | None,
    call_wall_1: float | None,
    call_wall_2: float | None,
) -> dict[str, Any]:
    """
    Check if TP1 is blocked by a call wall.
    Returns conflict flag, note, and adjusted TP if needed.
    """
    result = {
        "tp1_wall_conflict": False,
        "tp1_wall_note": None,
        "tp1_adjusted": None,
    }
    if tp1 is None or tp1 <= 0:
        return result

    # Check primary wall
    if call_wall_1 is not None and call_wall_1 > 0:
        pct = abs(tp1 - call_wall_1) / call_wall_1 * 100
        if pct <= 2:
            result["tp1_wall_conflict"] = True
            result["tp1_wall_note"] = (
                f"TP1 at ${tp1:.2f} sits on ${call_wall_1:.2f} "
                f"call wall — resistance likely"
            )
            # Adjusted TP = just before the wall (no ATR available here)
            # The caller should apply ATR adjustment if available
            result["tp1_adjusted"] = round(call_wall_1 * 0.99, 2)
            return result

    # Check secondary wall
    if call_wall_2 is not None and call_wall_2 > 0:
        pct = abs(tp1 - call_wall_2) / call_wall_2 * 100
        if pct <= 2:
            result["tp1_wall_conflict"] = True
            result["tp1_wall_note"] = (
                f"TP1 at ${tp1:.2f} near ${call_wall_2:.2f} "
                f"secondary call wall — watch resistance"
            )
            result["tp1_adjusted"] = round(call_wall_2 * 0.99, 2)

    return result


def compute_stop_wall_check(
    stop: float | None,
    put_wall_1: float | None,
) -> dict[str, Any]:
    """
    Check if stop has put wall support just below it.
    Returns support flag and note.
    """
    result = {"stop_wall_support": False, "stop_wall_note": None}
    if stop is None or stop <= 0 or put_wall_1 is None or put_wall_1 <= 0:
        return result

    # Put wall should be BELOW stop (floor), and stop should be within 2% above the wall
    if put_wall_1 < stop:
        pct = (stop - put_wall_1) / put_wall_1 * 100
        if pct <= 2:
            result["stop_wall_support"] = True
            result["stop_wall_note"] = (
                f"Stop at ${stop:.2f} has ${put_wall_1:.2f} "
                f"put wall support — strong floor"
            )
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def compute_options_structure(
    contracts: list[dict],
    price: float,
    tp1: float | None = None,
    stop: float | None = None,
    atr: float | None = None,
) -> dict[str, Any]:
    """
    Compute full options structure for a ticker.
    Returns all max pain, walls, gamma flip, and wall check fields.
    """
    if not contracts or price <= 0:
        return {
            "max_pain_weekly": None,
            "max_pain_monthly": None,
            "max_pain_expiry_weekly": None,
            "max_pain_expiry_monthly": None,
            "days_to_weekly_expiry": None,
            "days_to_monthly_expiry": None,
            "price_vs_max_pain_pct": None,
            "max_pain_signal": None,
            "call_wall_1": None,
            "call_wall_1_oi": None,
            "call_wall_2": None,
            "call_wall_2_oi": None,
            "put_wall_1": None,
            "put_wall_1_oi": None,
            "put_wall_2": None,
            "put_wall_2_oi": None,
            "tp1_wall_conflict": None,
            "tp1_wall_note": None,
            "tp1_adjusted": None,
            "stop_wall_support": None,
            "stop_wall_note": None,
            "gamma_flip_proxy": None,
            "price_vs_gamma_flip": None,
        }

    # Bucket by expiry
    buckets = bucket_by_expiry(contracts)
    sorted_exps = sort_expiry_by_dte(buckets)

    # Identify weekly (DTE <= 14) vs monthly (DTE > 14)
    weekly_exps = [e for e in sorted_exps if min(c.get("DTE", 999) for c in buckets.get(e, [])) <= 14]
    monthly_exps = [e for e in sorted_exps if e not in weekly_exps]

    weekly_mp = compute_max_pain_for_expiry(buckets.get(weekly_exps[0], [])) if weekly_exps else None
    monthly_mp = compute_max_pain_for_expiry(buckets.get(monthly_exps[0], [])) if monthly_exps else None

    # Fallback: if no weekly, use first available
    if weekly_mp is None and sorted_exps:
        weekly_mp = compute_max_pain_for_expiry(buckets.get(sorted_exps[0], []))

    # Walls from ALL contracts across expiries
    walls = compute_walls(contracts, price)

    # Gamma flip
    gf = compute_gamma_flip_proxy(contracts)

    # TP / Stop checks
    tp_check = compute_tp_wall_check(tp1, walls.get("call_wall_1"), walls.get("call_wall_2"))
    stop_check = compute_stop_wall_check(stop, walls.get("put_wall_1"))

    # Apply ATR-based TP adjustment if available and conflict exists
    tp_adjusted = tp_check.get("tp1_adjusted")
    if tp_adjusted is not None and atr is not None and atr > 0:
        tp_adjusted = round(tp_adjusted - (atr * 0.5), 2)
        tp_check["tp1_adjusted"] = tp_adjusted
        if tp_check.get("tp1_wall_note"):
            tp_check["tp1_wall_note"] += f"; adjusted TP to ${tp_adjusted:.2f}"

    # Price vs max pain
    max_pain_primary = weekly_mp["strike"] if weekly_mp else (monthly_mp["strike"] if monthly_mp else None)
    price_vs_mp_pct = None
    max_pain_signal = "UNKNOWN"
    if max_pain_primary is not None and max_pain_primary > 0:
        price_vs_mp_pct = round((price - max_pain_primary) / max_pain_primary * 100, 2)
        max_pain_signal = classify_max_pain(price, max_pain_primary)

    return {
        "max_pain_weekly": weekly_mp["strike"] if weekly_mp else None,
        "max_pain_monthly": monthly_mp["strike"] if monthly_mp else None,
        "max_pain_expiry_weekly": weekly_exps[0] if weekly_exps else None,
        "max_pain_expiry_monthly": monthly_exps[0] if monthly_exps else None,
        "days_to_weekly_expiry": weekly_mp["days_to_expiry"] if weekly_mp else None,
        "days_to_monthly_expiry": monthly_mp["days_to_expiry"] if monthly_mp else None,
        "price_vs_max_pain_pct": price_vs_mp_pct,
        "max_pain_signal": max_pain_signal,
        **walls,
        **tp_check,
        **stop_check,
        "gamma_flip_proxy": gf["strike"] if gf else None,
        "price_vs_gamma_flip": classify_gamma_flip(price, gf["strike"] if gf else None),
    }
