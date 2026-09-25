#!/usr/bin/env python3
"""Alpha Lab V1: append-only, non-trading prospective challenger research.

Definitions are intentionally fixed here.  This module neither imports broker
code nor changes production Stage1/Stage2, PMF, PEAD, or execution behavior.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from research_price_resolver import ResearchPriceResolver
from research_telemetry_common import RESEARCH_ROOT, next_market_session, read_json, session_offset, utc_now, write_immutable
from swing_shadow_cohorts import SECTOR_ETFS

ROOT = Path(__file__).resolve().parent
LAB_ROOT = RESEARCH_ROOT / "alpha_lab_v1"
REGISTRY = LAB_ROOT / "experiment_registry.json"
MOM = "MOMENTUM_CONTROL_V1"
CAT = "CATALYST_CONTINUATION_V1"
REV = "IDIOSYNCRATIC_REVERSAL_V1"
HORIZONS = (1, 3, 5, 10)
MOM_HORIZONS = (5, 10, 20, 40)


def num(value: Any) -> float | None:
    try:
        value = float(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def parse(value: Any) -> datetime | None:
    try:
        text = str(value or "").replace("Z", "+00:00")
        item = datetime.fromisoformat(text)
        return item if item.tzinfo else item.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def registry() -> dict[str, Any]:
    prior = read_json(REGISTRY, None)
    if isinstance(prior, dict):
        return prior
    now = utc_now().isoformat()
    definitions = {
        MOM: {"variants": {"MOM_6_1": "close[-21]/close[-126]-1", "MOM_12_1": "close[-21]/close[-252]-1"}, "entry": "NEXT_OPEN", "primary": "20D SPY-adjusted", "threshold_dates": 15},
        CAT: {"membership": "timestamped selected_top_30 catalyst, observed before entry, event age <=36h; Stage1 liquidity/data-quality only", "entries": ["NEXT_OPEN", "FIRST_ORDERLY_PULLBACK_PENDING_INTRADAY"], "primary": "5D SPY-adjusted", "threshold_dates": 15},
        REV: {"membership": "Stage1 liquid/data-quality rows; one-day stock return minus mean(SPY, sector ETF); fixed buckets 3-5% and >=5%; catalyst state separated", "entry": "NEXT_OPEN", "primary": "3D direction-adjusted", "threshold_dates": 15},
    }
    write_immutable(REGISTRY, {"schema_version": 1, "research_only": True, "non_trading": True, "deployed_at": now, "experiments": definitions, "modifications": []})
    return read_json(REGISTRY, {}) or {}


def latest(session: str, filename: str) -> Path | None:
    matches = sorted(RESEARCH_ROOT.glob(f"{session}/*/{filename}"))
    return matches[-1] if matches else None


def prior_close(resolver: ResearchPriceResolver, symbol: str, session: date, offset: int) -> float | None:
    target = session_offset(session, offset)
    if not target:
        return None
    return num(resolver.resolve(symbol, target["session_date"], "SESSION_CLOSE").get("price"))


def ret(resolver: ResearchPriceResolver, symbol: str, session: date, older: int, newer: int) -> float | None:
    first, last = prior_close(resolver, symbol, session, older), prior_close(resolver, symbol, session, newer)
    return (last / first - 1) if first and last else None


def rank_quintiles(rows: list[dict[str, Any]], field: str) -> None:
    valid = [row for row in rows if isinstance(row.get(field), (int, float))]
    valid.sort(key=lambda row: (-row[field], row["symbol"]))
    for i, row in enumerate(valid):
        row["rank"] = i + 1
        row["quintile"] = min(5, i * 5 // max(1, len(valid)) + 1)


def deterministic_control(rows: list[dict[str, Any]], row: dict[str, Any], seed: str) -> str | None:
    pool = [r for r in rows if r["symbol"] != row["symbol"] and r.get("sector") == row.get("sector")]
    if not pool:
        pool = [r for r in rows if r["symbol"] != row["symbol"]]
    if not pool:
        return None
    pool.sort(key=lambda r: r["symbol"])
    value = int(hashlib.sha256(f"{seed}|{row['symbol']}".encode()).hexdigest()[:16], 16)
    return pool[value % len(pool)]["symbol"]


def create(corrective: bool = False) -> dict[str, Any]:
    reg = registry(); deployed = parse(reg.get("deployed_at")); timing = next_market_session(); session = timing["trading_session"]
    existing = sorted((LAB_ROOT / session).glob("*/memberships.json"))
    if existing and not corrective:
        return {"ok": True, "idempotent": True, "path": str(existing[-1]), "broker_calls": 0}
    source = latest(session, "stage1_v2_shadow.json")
    if not source:
        return {"ok": True, "pending": "STAGE1_ARTIFACT_NOT_AVAILABLE", "broker_calls": 0}
    artifact = read_json(source, {}) or {}; source_time = parse(artifact.get("pipeline_timestamp") or artifact.get("run_timestamp"))
    run_at = utc_now().isoformat(); rows: list[dict[str, Any]] = []
    pre_entry_correction = corrective and utc_now() < datetime.fromisoformat(timing["next_session_open"])
    if not (deployed and source_time and (source_time >= deployed or pre_entry_correction)):
        path = LAB_ROOT / session / str(artifact.get("run_id") or "pending") / ("memberships_v1_1.json" if corrective else "memberships.json")
        write_immutable(path, {"schema_version": 1, "research_only": True, "non_trading": True, "immutable_membership": True, "reason": "PRE_DEPLOYMENT_SOURCE_NOT_ADMITTED", "rows": []})
        return {"ok": True, "rows": 0, "reason": "PRE_DEPLOYMENT_SOURCE_NOT_ADMITTED", "broker_calls": 0}
    eligible = [r for r in artifact.get("rows", []) if r.get("stage1_v2_classification") == "ALPHA_ELIGIBLE" and r.get("symbol")]
    symbols = {str(r["symbol"]).upper() for r in eligible}
    resolver = ResearchPriceResolver(symbols | {"SPY"} | set(SECTOR_ETFS.values()))
    day = date.fromisoformat(session); seed = f"ALPHA_LAB_V1|{session}"
    base = []
    for item in eligible:
        symbol = str(item["symbol"]).upper(); sector = item.get("sector")
        base.append({"symbol": symbol, "sector": sector, "dollar_adv": item.get("dollar_adv"), "spread_pct": item.get("spread_pct"), "source_lineage": item.get("source_lineage"), "stage1_artifact": str(source)})
    # Momentum ranks are derived and frozen from only prices preceding entry.
    momentum = []
    for item in base:
        six, twelve = ret(resolver, item["symbol"], day, -126, -21), ret(resolver, item["symbol"], day, -252, -21)
        for variant, score in (("MOM_6_1", six), ("MOM_12_1", twelve)):
            momentum.append({**item, "experiment_name": MOM, "variant": variant, "momentum_score": score, "trading_date": session, "next_open_timestamp": timing["next_session_open"], "entry_basis": "NEXT_OPEN", "model_reference": None, "estimated_executable": None, "real_fill": None, "membership_timestamp": run_at, "research_only": True, "non_trading": True})
    for variant in ("MOM_6_1", "MOM_12_1"):
        rank_quintiles([r for r in momentum if r["variant"] == variant], "momentum_score")
    rows.extend(momentum)
    # Catalyst membership is limited to timestamped, top-30 pre-cap events.
    catalyst = read_json(latest(session, "catalyst_pre_cap.json") or Path("/nonexistent"), {}) or {}
    cutoff = source_time - timedelta(hours=36)
    lookup = {r["symbol"]: r for r in base}
    for event in catalyst.get("candidates", []):
        symbol = str(event.get("ticker") or "").upper(); event_time = parse(event.get("event_timestamp"))
        if not (symbol in lookup and event.get("selected_top_30") and event.get("inside_base_universe") and event_time and cutoff <= event_time < source_time):
            continue
        rows.append({**lookup[symbol], "experiment_name": CAT, "variant": str(event.get("event_type") or "OTHER_TIMESTAMPED"), "event_timestamp": event_time.isoformat(), "event_source": event.get("source"), "reaction_pct": None, "gap_pct": None, "gap_atr": None, "mfe": None, "mae": None, "first_orderly_pullback_state": "PENDING_AUTHORITATIVE_INTRADAY_PATH", "trading_date": session, "next_open_timestamp": timing["next_session_open"], "entry_basis": "NEXT_OPEN", "model_reference": None, "estimated_executable": None, "real_fill": None, "membership_timestamp": run_at, "research_only": True, "non_trading": True})
    # Reversal uses a fixed idiosyncratic measure and never mixes catalyst states.
    spy_1d = ret(resolver, "SPY", day, -2, -1)
    for item in base:
        stock = ret(resolver, item["symbol"], day, -2, -1); etf = SECTOR_ETFS.get(str(item.get("sector") or "")); sector = ret(resolver, etf, day, -2, -1) if etf else None
        if stock is None or spy_1d is None or sector is None:
            continue
        value = stock - (spy_1d + sector) / 2
        magnitude = abs(value)
        if magnitude < .03:
            continue
        lineage = (item.get("source_lineage") or {}).get("candidate_metadata") or {}
        catalyst_state = "KNOWN_CATALYST" if lineage.get("catalyst_datetime") else "NO_MAJOR_CATALYST"
        bucket = "EXTREME_GE_5PCT" if magnitude >= .05 else "EXTREME_3_TO_5PCT"
        rows.append({**item, "experiment_name": REV, "variant": bucket, "catalyst_state": catalyst_state, "idiosyncratic_return": value, "direction": "SHORT_REVERSION" if value > 0 else "LONG_REVERSION", "trading_date": session, "next_open_timestamp": timing["next_session_open"], "entry_basis": "NEXT_OPEN", "model_reference": None, "estimated_executable": None, "real_fill": None, "membership_timestamp": run_at, "research_only": True, "non_trading": True})
    for row in rows:
        row["control_symbol"] = deterministic_control(base, row, seed)
    path = LAB_ROOT / session / str(artifact.get("run_id") or source_time.strftime("%Y%m%dT%H%M%SZ")) / ("memberships_v1_1.json" if corrective else "memberships.json")
    write_immutable(path, {"schema_version": 1, "research_only": True, "non_trading": True, "immutable_membership": True, "deployment_timestamp": reg.get("deployed_at"), "run_id": artifact.get("run_id"), "pipeline_timestamp": artifact.get("pipeline_timestamp"), "intended_xnys_session": session, "next_open_timestamp": timing["next_session_open"], "source_artifact": str(source), "control_seed": seed, "correction_of": "ALPHA_LAB_V1" if corrective else None, "correction_reason": "PRE_ENTRY_COLLECTOR_BUG" if corrective else None, "created_before_entry": pre_entry_correction, "original_v1_preserved": corrective, "rows": rows})
    return {"ok": True, "rows": len(rows), "momentum": len(momentum), "catalyst": sum(r["experiment_name"] == CAT for r in rows), "reversal": sum(r["experiment_name"] == REV for r in rows), "broker_calls": 0}


def label(row: dict[str, Any], resolver: ResearchPriceResolver, horizon: int) -> dict[str, Any]:
    start = date.fromisoformat(row["trading_date"]); target = session_offset(start, horizon)
    if not target:
        return {"state": "INVALID_SESSION"}
    entry, close = resolver.resolve(row["symbol"], start.isoformat(), "NEXT_OPEN"), resolver.resolve(row["symbol"], target["session_date"], "SESSION_CLOSE")
    if entry.get("price") is None: return {"state": "MISSING_ENTRY", "entry_provenance": entry}
    if close.get("price") is None: return {"state": "MISSING_TARGET_PRICE", "close_provenance": close, "target_market_date": target["session_date"]}
    raw = (close["price"] / entry["price"] - 1) * 100
    spy_a, spy_b = resolver.resolve("SPY", start.isoformat(), "NEXT_OPEN"), resolver.resolve("SPY", target["session_date"], "SESSION_CLOSE")
    result = {"state": "AVAILABLE", "raw_return_pct": raw, "entry": entry["price"], "close": close["price"], "target_market_date": target["session_date"], "entry_provenance": entry, "close_provenance": close}
    if spy_a.get("price") and spy_b.get("price"):
        spy = (spy_b["price"] / spy_a["price"] - 1) * 100; result.update({"spy_return_pct": spy, "spy_adjusted_return_pct": raw - spy})
    if row.get("experiment_name") == REV and row.get("direction") == "SHORT_REVERSION":
        result["direction_adjusted_return_pct"] = -raw
    else: result["direction_adjusted_return_pct"] = raw
    return result


def update() -> dict[str, Any]:
    members = []
    for path in sorted(LAB_ROOT.glob("*/*/memberships.json")):
        members.extend([{**r, "membership_artifact": str(path)} for r in (read_json(path, {}) or {}).get("rows", [])])
    symbols = {r["symbol"] for r in members} | {r["control_symbol"] for r in members if r.get("control_symbol")}
    resolver = ResearchPriceResolver(symbols | {"SPY"} | set(SECTOR_ETFS.values())) if symbols else None
    rows = []
    for row in members:
        horizons = MOM_HORIZONS if row["experiment_name"] == MOM else HORIZONS
        outcome = {f"d{h}": label(row, resolver, h) for h in horizons} if resolver else {}
        control = {"symbol": row.get("control_symbol"), "outcomes": {}}
        if resolver and row.get("control_symbol"):
            for h in horizons: control["outcomes"][f"d{h}"] = label({**row, "symbol": row["control_symbol"]}, resolver, h)
        rows.append({**row, "outcome_basis": "NEXT_OPEN", "outcomes": outcome, "matched_control": control})
    now = utc_now(); tag = now.strftime("%Y%m%dT%H%M%SZ")
    write_immutable(LAB_ROOT / "scoreboards" / f"alpha_lab_outcomes_{tag}.json", {"schema_version": 1, "research_only": True, "non_trading": True, "created_at": now.isoformat(), "rows": rows})
    report = []
    for name in (MOM, CAT, REV):
        group = [r for r in rows if r["experiment_name"] == name]; horizon = 20 if name == MOM else (5 if name == CAT else 3)
        values = [r["outcomes"].get(f"d{horizon}", {}).get("direction_adjusted_return_pct") for r in group]
        values = [v for v in values if isinstance(v, (int, float))]
        deltas = []
        for r in group:
            own = r["outcomes"].get(f"d{horizon}", {}).get("direction_adjusted_return_pct"); ctl = r["matched_control"]["outcomes"].get(f"d{horizon}", {}).get("direction_adjusted_return_pct")
            if isinstance(own, (int, float)) and isinstance(ctl, (int, float)): deltas.append(own - ctl)
        dates = len({r["trading_date"] for r in group}); status = "DESCRIPTIVE_ONLY" if dates < 15 else "EARLY_EVIDENCE" if dates < 30 else "PRELIMINARY" if dates < 60 else "MEANINGFUL_EVIDENCE"
        report.append({"strategy": name, "independent_dates": dates, "mature_rows": len(values), "primary_horizon": horizon, "primary_mean": statistics.fmean(values) if values else None, "median": statistics.median(values) if values else None, "win_rate": 100 * sum(v > 0 for v in values) / len(values) if values else None, "matched_control_delta": statistics.fmean(deltas) if deltas else None, "execution_coverage": "NEXT_OPEN_PROXY_ONLY", "status": status})
    # Existing frozen experiments remain untouched.  Their rows are carried as
    # explicit leaderboard placeholders so a tiny new cohort cannot appear to
    # be a winner merely by omission of the established controls.
    for name in ("STAGE1_NEXT_OPEN_BASELINE_V1", "FULL_STAGE2_QUARTILES_V1",
                 "CLUSTER_KEPT_NEXT_OPEN_V1", "PEAD_NEXT_OPEN_FIXED_HOLD_V1",
                 "PEAD_CURRENT_EXIT", "PMF_NEGATIVE_CONTROL"):
        report.append({"strategy": name, "independent_dates": None, "mature_rows": None,
                       "primary_horizon": None, "primary_mean": None, "median": None,
                       "win_rate": None, "max_drawdown": None, "matched_control_delta": None,
                       "execution_coverage": "SEPARATE_FROZEN_EXPERIMENT", "status": "COLLECTING"})
    summary = LAB_ROOT / "scoreboards" / f"alpha_leaderboard_{tag}.json"
    write_immutable(summary, {"schema_version": 1, "research_only": True, "non_trading": True, "created_at": now.isoformat(), "leaderboard": report})
    return {"ok": True, "rows": len(rows), "leaderboard": str(summary), "broker_calls": 0}


def weekly() -> dict[str, Any]:
    latest_score = sorted((LAB_ROOT / "scoreboards").glob("alpha_leaderboard_*.json"))
    payload = {"schema_version": 1, "research_only": True, "non_trading": True, "created_at": utc_now().isoformat(),
               "root_cause": "DESCRIPTIVE_ONLY_UNTIL_EVIDENCE_GATES", "leaderboard": read_json(latest_score[-1], {}) if latest_score else {},
               "weekly_questions": {"new_evidence": "REPORTED_FROM_LEADERBOARD", "improved_vs_control": "TOO_EARLY", "deteriorated": "TOO_EARLY", "alpha_loss": "TOO_EARLY", "loss_attribution": "TOO_EARLY", "checkpoint_reached": "NONE_UNLESS_LEDGER_SHOWS_15_DATES", "too_early": "ALL_NEW_ALPHA_LAB_EXPERIMENTS"},
               "broker_calls": 0}
    path = LAB_ROOT / "weekly" / f"alpha_lab_weekly_{utc_now().strftime('%G-W%V')}.json"
    write_immutable(path, payload); return {"ok": True, "path": str(path), "broker_calls": 0}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=("create", "create-corrective", "update", "weekly", "self-test")); args = parser.parse_args()
    funcs = {"create": create, "create-corrective": lambda: create(True), "update": update, "weekly": weekly, "self-test": lambda: {"ok": True, "research_only": True, "broker_calls": 0, "experiments": [MOM, CAT, REV]}}
    print(json.dumps(funcs[args.command](), sort_keys=True))


if __name__ == "__main__": main()
