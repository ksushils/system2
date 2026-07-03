#!/usr/bin/env python3
"""
System 2 — Self-learning weight engine.

Tracks which signals predict winners and adjusts scoring weights,
but ONLY once enough resolved trades exist. Below the sample-size
gates the engine is DORMANT: it observes and reports but never
touches live weights.

Gates (non-negotiable):
  < 30 resolved:   DORMANT  — report only, no weight changes
  30-99 resolved:  ADVISORY — gentle ±10% nudges, clearly provisional
  100-199 resolved: ACTIVE  — learned weights capped at ±25%
  200+ resolved:   FULL     — learned weights capped at ±40%
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parent
FUND_PATH = Path("/root/fund-system/data/fund.json")
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
LEDGER_PATH = DATA_DIR / "signal_outcome_ledger.json"
EDGE_PATH = DATA_DIR / "signal_edge_analysis.json"
WEIGHTS_PATH = DATA_DIR / "learned_weights.json"

# Family caps (confirmation scoring) and trade-quality weights.
# These are the THEORY weights the live system uses today.
THEORY_WEIGHTS: dict[str, float] = {
    "momentum": 25.0,
    "positioning": 35.0,
    "catalyst": 20.0,
    "structural": 20.0,
    "core_weight": 0.50,
    "confirmation_weight": 0.30,
    "risk_weight": 0.20,
}

# Signals we observe per resolved trade and the family they inform.
# Order matches the family weights above.
SIGNALS: list[tuple[str, str]] = [
    # Momentum family
    ("rvol", "momentum"),
    ("proximity_52wk", "momentum"),
    ("adx", "momentum"),
    ("setup_type", "momentum"),
    ("pre_market_status", "momentum"),
    # Positioning / flow family
    ("options_verdict", "positioning"),
    ("dark_pool_signal", "positioning"),
    ("insider_buy_signal", "positioning"),
    # Catalyst family
    ("pead_score", "catalyst"),
    ("social_sentiment", "catalyst"),
    ("congress_signal", "catalyst"),
    ("analyst_rec_direction", "catalyst"),
    ("danelfin_aiscore", "catalyst"),
    # Structural / regime family
    ("regime", "structural"),
]

SIGNAL_NAMES = [s[0] for s in SIGNALS]


def num(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", "None", "null"):
            return default
        v = float(value)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def text(value: Any) -> str:
    return str(value or "").strip().upper()


def load_config() -> dict[str, Any]:
    """Load system config; create a default file if missing."""
    default = {"self_learning_enabled": True}
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(default, indent=2), encoding="utf-8")
        return default.copy()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default.copy()
        return {**default, **data}
    except Exception:
        return default.copy()


def load_fund() -> dict[str, Any]:
    return json.loads(FUND_PATH.read_text(encoding="utf-8"))


def _get_value(trade: dict[str, Any], name: str) -> Any:
    """Resolve a signal value, handling aliases."""
    if name == "rvol":
        return trade.get("rvol") if trade.get("rvol") is not None else trade.get("volumeRatio")
    if name == "proximity_52wk":
        return trade.get("proximity_52wk") if trade.get("proximity_52wk") is not None else trade.get("pct_from_52wk_high")
    if name == "regime":
        return trade.get("market_regime") if trade.get("market_regime") is not None else trade.get("regime")
    return trade.get(name)


def signal_present(trade: dict[str, Any], name: str) -> bool:
    """Return True when the signal is considered 'present/strong'."""
    value = _get_value(trade, name)
    t = text(value)
    n = num(value)

    if name == "rvol":
        return n is not None and n >= 2.0
    if name == "proximity_52wk":
        # Close to 52-week high (within 10%) or deep value (<=-30)
        return n is not None and (n >= -10 or n <= -30)
    if name == "adx":
        return n is not None and n >= 25.0
    if name == "setup_type":
        return bool(value) and t not in ("", "UNKNOWN", "NONE", "NULL")
    if name == "pre_market_status":
        return "FAVOURABLE" in t or "FAVORABLE" in t or value is True
    if name == "options_verdict":
        return t in ("CONFIRM", "STRONG_CONFIRM", "BULLISH")
    if name == "dark_pool_signal":
        return t in ("STRONG", "BULLISH", "BUY")
    if name == "insider_buy_signal":
        return t in ("YES", "STRONG", "BUY", "TRUE") or value is True
    if name == "pead_score":
        return n is not None and n >= 70.0
    if name == "social_sentiment":
        return t in ("BULLISH", "POSITIVE") or (n is not None and n > 0)
    if name == "congress_signal":
        return t in ("BUY", "STRONG")
    if name == "analyst_rec_direction":
        return t in ("BUY", "UPGRADE", "BULLISH", "STRONG_BUY")
    if name == "danelfin_aiscore":
        return n is not None and n >= 70.0
    if name == "regime":
        return t in ("TRENDING", "BULLISH", "RISK_ON")

    # Fallback: truthy non-empty value
    return bool(value) and t not in ("", "UNKNOWN", "NONE", "NULL", "FALSE", "NO")


def _outcome_r(trade: dict[str, Any]) -> float | None:
    """Return resolved R, preferring actual then paper."""
    r = trade.get("actual_r")
    if r is None:
        r = trade.get("paper_exit_r")
    if r is None:
        return None
    try:
        return float(r)
    except Exception:
        return None


def build_ledger() -> list[dict[str, Any]]:
    """Snapshot signals for every resolved v2-era trade."""
    db = load_fund()
    ideas = db.get("ideas", [])

    ledger: list[dict[str, Any]] = []
    for idea in ideas:
        if idea.get("date", "") < "2026-06-09":
            continue
        if idea.get("paper_status") == "INVALID" or idea.get("r_calculation_suspect") is True:
            continue
        r = _outcome_r(idea)
        if r is None:
            continue
        if abs(r) > MAX_SANE_ABS_R:
            continue

        record = {
            "ticker": idea.get("ticker"),
            "date": idea.get("date"),
            "outcome_r": round(r, 4),
            "won": r > 0,
            "signals": {},
        }
        for name, _ in SIGNALS:
            record["signals"][name] = _get_value(idea, name)
        ledger.append(record)

    return ledger


def analyse_signal_edge(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute per-signal edge from the ledger."""
    results: dict[str, Any] = {}

    for name, _ in SIGNALS:
        with_signal = [t for t in ledger if signal_present(t, name)]
        without = [t for t in ledger if not signal_present(t, name)]

        if len(with_signal) < 3:
            results[name] = {
                "status": "INSUFFICIENT_DATA",
                "sample_with": len(with_signal),
                "sample_without": len(without),
            }
            continue

        rs_with = [t["outcome_r"] for t in with_signal]
        rs_without = [t["outcome_r"] for t in without] if without else []

        avg_with = mean(rs_with)
        avg_without = mean(rs_without) if rs_without else 0.0
        edge = avg_with - avg_without
        wr_with = len([r for r in rs_with if r > 0]) / len(rs_with) * 100
        sample = len(with_signal)

        results[name] = {
            "status": "OK",
            "sample_with": sample,
            "sample_without": len(without),
            "avg_r_with": round(avg_with, 3),
            "avg_r_without": round(avg_without, 3),
            "edge": round(edge, 3),
            "win_rate_with": round(wr_with, 1),
            "confidence": (
                "HIGH" if sample >= 30 else
                "MEDIUM" if sample >= 15 else
                "LOW" if sample >= 8 else
                "VERY_LOW"
            ),
        }

    return results


def _family_edge_analysis(edge_analysis: dict[str, Any]) -> dict[str, Any]:
    """Aggregate per-signal edges into family-level edges."""
    family_edges: dict[str, list[float]] = {}
    family_samples: dict[str, list[int]] = {}

    for name, family in SIGNALS:
        data = edge_analysis.get(name, {})
        if data.get("status") != "OK":
            continue
        edge = data.get("edge", 0.0)
        sample = data.get("sample_with", 0)
        family_edges.setdefault(family, []).append(edge)
        family_samples.setdefault(family, []).append(sample)

    results: dict[str, Any] = {}
    for family in THEORY_WEIGHTS:
        # Core/risk/confirmation are not family signals; leave untouched.
        if family not in family_edges:
            continue
        edges = family_edges[family]
        samples = family_samples[family]
        total = sum(samples)
        # Weighted-average edge by sample size within the family
        if total > 0:
            w_edge = sum(e * s for e, s in zip(edges, samples)) / total
        else:
            w_edge = 0.0
        results[family] = {
            "signals_considered": len(edges),
            "sample_with": total,
            "edge": round(w_edge, 3),
            "confidence": (
                "HIGH" if total >= 30 else
                "MEDIUM" if total >= 15 else
                "LOW" if total >= 8 else
                "VERY_LOW"
            ),
        }
    return results


def load_current_weights() -> dict[str, float]:
    """Return the current theory weights."""
    return THEORY_WEIGHTS.copy()


def compute_learned_weights(
    edge_analysis: dict[str, Any],
    total_resolved: int,
) -> dict[str, Any]:
    """
    Translate measured edge into weight adjustments.
    HARD GATED by total_resolved count.
    """
    theory = load_current_weights()
    family_edges = _family_edge_analysis(edge_analysis)

    # GATE 1: Below 30 — DORMANT
    if total_resolved < 30:
        return {
            "mode": "DORMANT",
            "reason": (
                f"Only {total_resolved} resolved trades. Need 30 to begin. "
                "Theory weights unchanged."
            ),
            "cap": 0.0,
            "weights": theory,
            "theory_weights": theory,
            "adjustments_applied": False,
            "total_resolved": total_resolved,
            "family_edge_analysis": family_edges,
        }

    # GATE 2: 30-99 — ADVISORY (±10% max nudge)
    if total_resolved < 100:
        cap = 0.10
        mode = "ADVISORY"
    # GATE 3: 100-199 — ACTIVE (±25% max)
    elif total_resolved < 200:
        cap = 0.25
        mode = "ACTIVE"
    # GATE 4: 200+ — FULL (±40% max, still capped)
    else:
        cap = 0.40
        mode = "FULL"

    config = load_config()
    if not config.get("self_learning_enabled", True):
        return {
            "mode": "DISABLED",
            "reason": "self_learning_enabled is false in config.json.",
            "cap": 0.0,
            "weights": theory,
            "theory_weights": theory,
            "adjustments_applied": False,
            "total_resolved": total_resolved,
            "family_edge_analysis": family_edges,
        }

    learned: dict[str, float] = {}
    for key, theory_w in theory.items():
        edge_data = family_edges.get(key)
        if edge_data is None:
            learned[key] = theory_w
            continue

        edge = edge_data.get("edge", 0.0)
        conf = edge_data.get("confidence", "VERY_LOW")

        # Only adjust signals with adequate sample
        if conf == "VERY_LOW":
            learned[key] = theory_w
            continue

        # Edge-proportional adjustment, capped
        adjustment = max(min(edge * 0.5, cap), -cap)
        learned[key] = round(theory_w * (1 + adjustment), 3)

    return {
        "mode": mode,
        "cap": cap,
        "weights": learned,
        "theory_weights": theory,
        "adjustments_applied": True,
        "total_resolved": total_resolved,
        "family_edge_analysis": family_edges,
    }


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def run() -> dict[str, Any]:
    """Rebuild ledger, edge analysis, and learned weights."""
    ledger = build_ledger()
    edge_analysis = analyse_signal_edge(ledger)
    total_resolved = len(ledger)
    weights = compute_learned_weights(edge_analysis, total_resolved)

    # Add metadata
    weights["generated_at"] = datetime.now(timezone.utc).isoformat()
    weights["ledger_count"] = total_resolved
    weights["signal_edge_analysis"] = edge_analysis

    save_json(LEDGER_PATH, ledger)
    save_json(EDGE_PATH, edge_analysis)
    save_json(WEIGHTS_PATH, weights)

    print(f"Resolved trades: {total_resolved}")
    print(f"Mode: {weights['mode']}")
    print(f"Adjustments applied: {weights['adjustments_applied']}")
    if weights["mode"] != "DORMANT":
        print(f"Cap: {weights['cap']}")
    return weights


def build_test_ledger(n: int = 35, seed: int | None = None) -> list[dict[str, Any]]:
    """Generate a synthetic ledger for unit testing the gates."""
    import random
    rng = random.Random(seed)
    ledger: list[dict[str, Any]] = []
    base_date = datetime(2026, 6, 9)
    for i in range(n):
        r = round(rng.gauss(0.5, 1.5), 3)
        ledger.append({
            "ticker": f"TST{i:03d}",
            "date": (base_date).strftime("%Y-%m-%d"),
            "outcome_r": r,
            "won": r > 0,
            "signals": {
                "rvol": rng.choice([1.2, 2.5, 4.0]),
                "proximity_52wk": rng.uniform(-25, -5),
                "adx": rng.choice([20.0, 30.0]),
                "setup_type": rng.choice(["BREAKOUT", "PULLBACK"]),
                "pre_market_status": rng.choice(["NEUTRAL", "FAVOURABLE"]),
                "options_verdict": rng.choice(["NEUTRAL", "CONFIRM"]),
                "dark_pool_signal": rng.choice(["NORMAL", "STRONG"]),
                "insider_buy_signal": rng.choice(["NO", "YES"]),
                "pead_score": rng.choice([50.0, 80.0]),
                "social_sentiment": rng.choice(["NEUTRAL", "BULLISH"]),
                "congress_signal": rng.choice(["NONE", "BUY"]),
                "analyst_rec_direction": rng.choice(["HOLD", "BUY"]),
                "danelfin_aiscore": rng.choice([60.0, 85.0]),
                "regime": rng.choice(["NORMAL", "TRENDING"]),
            },
        })
        base_date += __import__("datetime").timedelta(days=1)
    return ledger


if __name__ == "__main__":
    run()
