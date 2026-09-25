#!/usr/bin/env python3
"""Append-only, non-trading alpha-isolation experiments.

This collector deliberately has no broker client, order code, or production
selection imports.  It freezes two prospective populations and evaluates them
only with Research Measurement V2 canonical prices.
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from research_price_resolver import ResearchPriceResolver
from research_telemetry_common import (
    RESEARCH_ROOT, authority_fields, next_market_session, read_json,
    run_directory, session_offset, utc_now, write_immutable,
)
from swing_shadow_cohorts import SECTOR_ETFS

ROOT = Path(__file__).resolve().parent
EXPERIMENT_ROOT = RESEARCH_ROOT / "alpha_edge_isolation_v1"
REGISTRY = EXPERIMENT_ROOT / "deployment_registry.json"
PEAD = "PEAD_NEXT_OPEN_FIXED_HOLD_V1"
STAGE1 = "STAGE1_NEXT_OPEN_BASELINE_V1"
PEAD_HORIZONS = (1, 3, 5, 10, 20)
STAGE1_HORIZONS = (1, 3, 5, 7)


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def stamp(value: Any) -> datetime | None:
    try:
        text = str(value or "").replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def deployment() -> dict[str, Any]:
    existing = read_json(REGISTRY, None)
    if isinstance(existing, dict):
        return existing
    now = utc_now()
    return_json = {
        "schema_version": 1, "research_only": True, "non_trading": True,
        "deployed_at": now.isoformat(), "experiments": [PEAD, STAGE1],
        "rules": {PEAD: "new PEAD events only; next XNYS open; fixed holds",
                  STAGE1: "new structural/data-quality Stage1 eligible rows only; next XNYS open"},
    }
    write_immutable(REGISTRY, return_json)
    return read_json(REGISTRY, return_json)


def latest_stage1(session: str) -> Path | None:
    matches = sorted(RESEARCH_ROOT.glob(f"{session}/*/stage1_v2_shadow.json"))
    return matches[-1] if matches else None


def existing_membership(session: str, name: str) -> Path | None:
    matches = sorted((EXPERIMENT_ROOT / session).glob(f"*/{name}"))
    return matches[-1] if matches else None


def create() -> dict[str, Any]:
    registry = deployment()
    deployed_at = stamp(registry.get("deployed_at"))
    timing = next_market_session()
    session = timing["trading_session"]
    run_at = utc_now().isoformat()
    run_fields = authority_fields(session, f"{run_at}-alpha-isolation", run_at)
    directory = EXPERIMENT_ROOT / session / str(run_fields["run_id"])
    directory.mkdir(parents=True, exist_ok=True)
    created: dict[str, Any] = {"broker_calls": 0, "session": session}

    # Stage1 baseline only begins with a source pipeline completed after the
    # deployment marker; earlier retained runs remain historical evidence.
    stage_path = latest_stage1(session)
    if stage_path and not existing_membership(session, "stage1_next_open_membership.json"):
        payload = read_json(stage_path, {}) or {}
        source_time = stamp(payload.get("pipeline_timestamp") or payload.get("run_timestamp"))
        rows = []
        if deployed_at and source_time and source_time >= deployed_at:
            for row in payload.get("rows", []):
                if row.get("stage1_v2_classification") != "ALPHA_ELIGIBLE":
                    continue
                symbol = str(row.get("symbol") or "").upper()
                if not symbol:
                    continue
                rows.append({
                    "experiment_name": STAGE1, "symbol": symbol, "trading_date": session,
                    "next_open_timestamp": timing["next_session_open"], "entry_source": "NEXT_OPEN",
                    "entry_provenance_state": "PENDING_NEXT_OPEN", "membership_timestamp": run_at,
                    "pipeline_timestamp": payload.get("pipeline_timestamp"), "pipeline_run_id": payload.get("run_id"),
                    "config_hash": payload.get("config_hash"), "sector": row.get("sector"),
                    "stage1_classification": row.get("stage1_v2_classification"),
                    "production_stage1_result": row.get("production_stage1_v1_result"),
                    "stage2_selected": None, "cluster_kept": None, "finalist": None,
                    "source_artifact": str(stage_path), "research_only": True, "non_trading": True,
                    **run_fields,
                })
        path = write_immutable(directory / "stage1_next_open_membership.json", {
            "schema_version": 1, "research_only": True, "non_trading": True,
            "immutable_membership": True, "experiment_name": STAGE1, **timing, **run_fields,
            "deployment_timestamp": registry.get("deployed_at"), "source_artifact": str(stage_path), "rows": rows,
        })
        created[STAGE1] = {"path": str(path), "rows": len(rows)}

    # PEAD rows are read from the ledger only.  A row is admitted only when its
    # point-in-time logging timestamp is after deployment and the live PEAD
    # event fields are present; no historical row is rewritten or backfilled.
    if not existing_membership(session, "pead_next_open_membership.json"):
        fund = read_json(ROOT.parent / "fund-system" / "data" / "fund.json", {}) or {}
        rows = []
        for event in fund.get("pead_drift_paper", []):
            logged = stamp(event.get("logged_at"))
            symbol = str(event.get("ticker") or "").upper()
            event_time = event.get("earnings_date") or event.get("date")
            genuine = (number(event.get("earnings_surprise_pct")) is not None and
                       number(event.get("reaction_return_pct")) is not None and
                       number(event.get("entry")) is not None and number(event.get("stop")) is not None)
            if not (symbol and genuine and deployed_at and logged and logged >= deployed_at):
                continue
            event_session = str(event_time or "")[:10]
            if not event_session:
                continue
            entry_session = next_market_session(logged)["trading_session"]
            rows.append({
                "experiment_name": PEAD, "event_id": event.get("id"), "symbol": symbol,
                "event_timestamp": logged.isoformat(), "event_session": event_session,
                "trading_date": entry_session, "next_open_timestamp": next_market_session(logged)["next_session_open"],
                "entry_source": "NEXT_OPEN", "entry_provenance_state": "PENDING_NEXT_OPEN",
                "membership_timestamp": run_at, "existing_pead_entry": event.get("entry"),
                "existing_pead_stop": event.get("stop"), "existing_pead_status": event.get("paper_status"),
                "existing_pead_exit": event.get("paper_exit_price"), "existing_pead_exit_r": event.get("canonical_r"),
                "existing_pead_exit_reason": event.get("paper_exit_reason"), "existing_pead_exit_at": event.get("paper_exit_at"),
                "tp1_breakeven_shadow_state": event.get("tp1_breakeven_shadow_state"),
                "existing_markouts": {key: event.get(key) for key in ("markout_r_current", "markout_r_10d", "markout_r_20d", "markout_r_40d", "markout_r_60d")},
                "risk_reference": event.get("risk_per_share"), "sector": event.get("sector"),
                "earnings_surprise_pct": event.get("earnings_surprise_pct"),
                "reaction_return_pct": event.get("reaction_return_pct"), "source_ledger_row": event.get("id"),
                "research_only": True, "non_trading": True, **authority_fields(entry_session, str(event.get("id")), logged.isoformat()),
            })
        path = write_immutable(directory / "pead_next_open_membership.json", {
            "schema_version": 1, "research_only": True, "non_trading": True,
            "immutable_membership": True, "experiment_name": PEAD, **timing, **run_fields,
            "deployment_timestamp": registry.get("deployed_at"), "rows": rows,
        })
        created[PEAD] = {"path": str(path), "rows": len(rows)}
    return {"ok": True, **created}


def outcome(row: dict[str, Any], resolver: ResearchPriceResolver, horizon: int) -> dict[str, Any]:
    start = date.fromisoformat(str(row["trading_date"])[:10])
    target = session_offset(start, horizon)
    if not target:
        return {"state": "INVALID_SESSION", "reason": "ENTRY_SESSION_INVALID"}
    entry = resolver.resolve(row["symbol"], start.isoformat(), "NEXT_OPEN")
    close = resolver.resolve(row["symbol"], target["session_date"], "SESSION_CLOSE")
    if entry.get("price") is None:
        return {"state": "MISSING_ENTRY", "entry_provenance": entry, "target_market_date": target["session_date"]}
    if close.get("price") is None:
        return {"state": "MISSING_TARGET_PRICE", "entry": entry["price"], "entry_provenance": entry, "close_provenance": close, "target_market_date": target["session_date"]}
    spy_entry, spy_close = resolver.resolve("SPY", start.isoformat(), "NEXT_OPEN"), resolver.resolve("SPY", target["session_date"], "SESSION_CLOSE")
    raw = (close["price"] / entry["price"] - 1) * 100
    result = {"state": "AVAILABLE", "entry": entry["price"], "close": close["price"], "target_market_date": target["session_date"], "raw_return_pct": raw, "entry_provenance": entry, "close_provenance": close}
    if spy_entry.get("price") and spy_close.get("price"):
        spy = (spy_close["price"] / spy_entry["price"] - 1) * 100
        result.update({"spy_return_pct": spy, "spy_adjusted_return_pct": raw - spy})
    else:
        result["benchmark_state"] = "BENCHMARK_MISSING"
    etf = SECTOR_ETFS.get(str(row.get("sector") or ""))
    if etf:
        sector_entry, sector_close = resolver.resolve(etf, start.isoformat(), "NEXT_OPEN"), resolver.resolve(etf, target["session_date"], "SESSION_CLOSE")
        if sector_entry.get("price") and sector_close.get("price"):
            sector = (sector_close["price"] / sector_entry["price"] - 1) * 100
            result.update({"sector_return_pct": sector, "sector_adjusted_return_pct": raw - sector})
    risk = number(row.get("risk_reference"))
    if risk:
        result["r_style_outcome"] = (close["price"] - entry["price"]) / risk
    return result


def collect_rows(filename: str) -> list[tuple[Path, dict[str, Any]]]:
    output = []
    for path in sorted(EXPERIMENT_ROOT.glob(f"*/*/{filename}")):
        for row in (read_json(path, {}) or {}).get("rows", []):
            output.append((path, row))
    return output


def update() -> dict[str, Any]:
    source = [(PEAD, "pead_next_open_membership.json", PEAD_HORIZONS), (STAGE1, "stage1_next_open_membership.json", STAGE1_HORIZONS)]
    all_rows = []
    for name, filename, horizons in source:
        members = collect_rows(filename)
        symbols = {str(row.get("symbol") or "").upper() for _, row in members if row.get("symbol")}
        resolver = ResearchPriceResolver(symbols | {"SPY"} | set(SECTOR_ETFS.values())) if symbols else None
        for path, row in members:
            enriched = {**row, "membership_artifact": str(path), "outcome_basis": "NEXT_OPEN", "outcomes": {}}
            for horizon in horizons:
                enriched["outcomes"][f"d{horizon}"] = outcome(enriched, resolver, horizon) if resolver else {"state": "MISSING_ENTRY", "reason": "NO_MEMBERS"}
            if name == PEAD:
                # Fixed-hold close outcomes do not need intraday ordering.  Path
                # metrics are explicitly ambiguous until an authoritative bar
                # path is retained; no favorable sequence is manufactured.
                enriched["path_metrics"] = {
                    "state": "AMBIGUOUS_PATH",
                    "reason": "NO_AUTHORITATIVE_INTRADAY_ORDERING_IN_FIXED_HOLD_COLLECTOR",
                    "mfe": None, "mae": None, "time_to_plus_0_5r": None,
                    "time_to_plus_1_0r": None, "time_to_plus_1_5r": None,
                    "time_to_minus_0_5r": None, "time_to_minus_1_0r": None,
                    "maximum_favorable_before_current_stop": None,
                }
            all_rows.append(enriched)
    now = utc_now(); stamp_text = now.strftime("%Y%m%dT%H%M%SZ")
    outcomes = write_immutable(EXPERIMENT_ROOT / "scoreboards" / f"alpha_edge_isolation_outcomes_{stamp_text}.json", {
        "schema_version": 1, "research_only": True, "non_trading": True, "created_at": now.isoformat(), "rows": all_rows,
    })
    report = []
    for name, _, horizons in source:
        group = [r for r in all_rows if r.get("experiment_name") == name]
        item = {"experiment": name, "n": len(group), "independent_dates": len({r.get("trading_date") for r in group}), "evidence_state": "DESCRIPTIVE_ONLY"}
        for horizon in horizons:
            vals = [r["outcomes"][f"d{horizon}"].get("raw_return_pct") for r in group if isinstance(r["outcomes"].get(f"d{horizon}"), dict) and isinstance(r["outcomes"][f"d{horizon}"].get("raw_return_pct"), (int, float))]
            item[f"d{horizon}_mean"] = statistics.fmean(vals) if vals else None
            item[f"d{horizon}_median"] = statistics.median(vals) if vals else None
        report.append(item)
    pead_rows = [r for r in all_rows if r.get("experiment_name") == PEAD]
    stopped = [r for r in pead_rows if r.get("existing_pead_exit_reason") == "STOP"]
    stopped_summary = {"stopped_events": len(stopped)}
    for horizon in (5, 10, 20):
        values = [r["outcomes"].get(f"d{horizon}", {}).get("raw_return_pct") for r in stopped]
        valid = [value for value in values if isinstance(value, (int, float))]
        stopped_summary[f"d{horizon}_positive_count"] = sum(value > 0 for value in valid)
        stopped_summary[f"d{horizon}_n"] = len(valid)
        stopped_summary[f"d{horizon}_mean"] = statistics.fmean(valid) if valid else None
    summary = write_immutable(EXPERIMENT_ROOT / "scoreboards" / f"alpha_edge_isolation_daily_{stamp_text}.json", {
        "schema_version": 1, "research_only": True, "non_trading": True, "created_at": now.isoformat(),
        "review_gates": {PEAD: "15 reaction dates; serious exit review 40 events / 30 dates", STAGE1: "15 mature +5D dates"},
        "experiments": report, "pead_stopped_fixed_hold": stopped_summary,
    })
    return {"ok": True, "rows": len(all_rows), "outcomes": str(outcomes), "summary": str(summary), "broker_calls": 0}


def self_test() -> dict[str, Any]:
    required = {"PEAD": PEAD, "STAGE1": STAGE1, "pead_horizons": PEAD_HORIZONS, "stage1_horizons": STAGE1_HORIZONS}
    return {"ok": True, "research_only": True, "broker_calls": 0, "required": required}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=("create", "update", "self-test")); args = parser.parse_args()
    print(json.dumps({"create": create, "update": update, "self-test": self_test}[args.command](), sort_keys=True))


if __name__ == "__main__":
    main()
