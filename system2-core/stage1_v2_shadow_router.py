#!/usr/bin/env python3
"""Immutable Stage1 V2 shadow routing and Research Measurement V2 outcomes.

This module is deliberately isolated from the production pipeline and contains
no broker imports or order-submission capability.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import statistics
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from research_price_resolver import ResearchPriceResolver
from research_telemetry_common import RESEARCH_ROOT, next_market_session, read_json, run_directory, utc_now, write_immutable
from swing_shadow_cohorts import HORIZONS, SECTOR_ETFS, label

ROOT = Path(__file__).resolve().parent
COHORTS = (
    "STAGE1_V2_ALPHA_ELIGIBLE",
    "STAGE1_V2_CAPACITY_LIMITED",
    "STAGE1_V2_EVENT_ROUTE",
    "STAGE1_V1_PASS_CONTROL",
)
BLOCKED_TICKERS = {"STRC"}
LIQUIDITY_REASONS = {"avg_volume_below_1m", "dollar_volume_below_20m"}
STRUCTURAL_REASONS = {"fund_or_etf", "inactive", "blocked_non_common_equity", "unsupported_security_type", "price_below_5"}


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result >= 0 else None
    except (TypeError, ValueError):
        return None


def symbol(row: dict[str, Any]) -> str:
    return re.sub(r"[^A-Z0-9.]", "", str(row.get("symbol") or row.get("ticker") or "").upper()).strip(".")


def load_env() -> None:
    for path in (ROOT / ".env",):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def get_json(endpoint: str, api_key: str) -> Any:
    separator = "&" if "?" in endpoint else "?"
    url = f"https://financialmodelingprep.com/{endpoint}{separator}apikey={urllib.parse.quote(api_key)}"
    request = urllib.request.Request(url, headers={"User-Agent": "System2-Stage1V2-Research/1.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def fresh_measurements(symbols: list[str], api_key: str) -> tuple[dict[str, dict[str, Any]], list[str], str]:
    rows: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    observed_at = utc_now().isoformat()
    ordered = sorted(set(symbols))
    for start in range(0, len(ordered), 75):
        batch = ordered[start:start + 75]
        try:
            payload = get_json("stable/batch-quote?symbols=" + ",".join(batch), api_key)
            if not isinstance(payload, list):
                raise ValueError("NON_LIST_BATCH_QUOTE_RESPONSE")
            for quote in payload:
                key = symbol(quote)
                if key:
                    rows[key] = quote
        except Exception as exc:
            errors.append(f"batch_{start // 75 + 1}:{type(exc).__name__}:{exc}")
    return rows, errors, observed_at


def event_calendar(api_key: str, days: int = 7) -> tuple[dict[str, dict[str, Any]], str | None]:
    today = date.today()
    try:
        payload = get_json(f"stable/earnings-calendar?from={today.isoformat()}&to={(today + timedelta(days=days)).isoformat()}", api_key)
        return ({symbol(row): row for row in payload if isinstance(row, dict) and symbol(row)} if isinstance(payload, list) else {}), None
    except Exception as exc:
        return {}, f"{type(exc).__name__}:{exc}"


def screener_enrichment(api_key: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Obtain ADV/profile fields in two broad calls, including recent-shadow rows."""
    result: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for exchange in ("NASDAQ", "NYSE"):
        try:
            payload = get_json(f"stable/company-screener?exchange={exchange}&limit=10000", api_key)
            if not isinstance(payload, list):
                raise ValueError("NON_LIST_SCREENER_RESPONSE")
            for row in payload:
                key = symbol(row)
                if key:
                    result[key] = row
        except Exception as exc:
            errors.append(f"{exchange}:{type(exc).__name__}:{exc}")
    return result, errors


def hash_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes() if path.exists() else b"MISSING")
    return digest.hexdigest()


def retained_corporate_actions(symbols: set[str], start_date: str, end_date: str) -> dict[str, list[dict[str, Any]]]:
    """Index only action cache files; never load the multi-gigabyte EOD cache."""
    result: dict[str, list[dict[str, Any]]] = {}
    wanted = {item.upper() for item in symbols}
    for pattern in ("*split*json", "*symbol-change*json", "*merger*json"):
        for raw_path in glob.glob(str(ROOT / "data/fmp_cache/*" / pattern)):
            path = Path(raw_path)
            payload = read_json(path, [])
            rows = payload.get("data", []) if isinstance(payload, dict) else payload
            for row in rows if isinstance(rows, list) else []:
                key = symbol(row)
                day = str(row.get("date") or row.get("effectiveDate") or "")[:10]
                if key in wanted and start_date <= day <= end_date:
                    result.setdefault(key, []).append({"date": day, "type": row.get("type") or path.name, "source_file": str(path)})
    return result


def classify(inputs: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    reasons = list(inputs.get("v1_reasons") or [])
    flags: list[str] = []
    if inputs.get("recent_shadow"):
        flags.append("RECENT_SHADOW_CONTEXT")
    dollar_adv = inputs.get("dollar_adv")
    if dollar_adv is not None and dollar_adv < 5_000_000:
        flags.append("EXTREME_LOW_LIQUIDITY")
    if inputs.get("source_failure") or inputs.get("price") in (None, 0) or inputs.get("adv") in (None, 0):
        return "REJECT_DATA_QUALITY", ["critical_point_in_time_measurement_missing_or_source_failure"], flags
    if inputs.get("corporate_action_unresolved"):
        return "REJECT_STRUCTURAL", ["unresolved_corporate_action_state"], flags
    structural = [reason for reason in reasons if reason in STRUCTURAL_REASONS]
    if inputs.get("blocked") and "blocked_non_common_equity" not in structural:
        structural.append("blocked_non_common_equity")
    if inputs.get("identity_unresolved"):
        structural.append("unresolved_instrument_identity")
    if inputs.get("price", 0) < 5 and "price_below_5" not in structural:
        structural.append("price_below_5")
    if structural:
        return "REJECT_STRUCTURAL", sorted(set(structural)), flags
    if inputs.get("event"):
        return "EVENT_ROUTE", ["recognized_binary_event_inside_swing_window"], flags
    if inputs.get("adv", 0) < 1_000_000 or inputs.get("dollar_adv", 0) < 20_000_000:
        return "ALPHA_ELIGIBLE_CAPACITY_LIMITED", sorted(set(reason for reason in reasons if reason in LIQUIDITY_REASONS) or {"v2_capacity_below_v1_gate"}), flags
    return "ALPHA_ELIGIBLE", [], flags


def latest_existing(session: str, filename: str) -> Path | None:
    paths = sorted((RESEARCH_ROOT / session).glob(f"*/{filename}"))
    return paths[-1] if paths else None


def create() -> dict[str, Any]:
    timing = next_market_session()
    session = timing["trading_session"]
    existing = latest_existing(session, "stage1_v2_shadow.json")
    if existing:
        payload = read_json(existing, {}) or {}
        return {"ok": True, "idempotent": True, "path": str(existing), "counts": payload.get("counts")}
    candidates = read_json(ROOT / "candidate_pool.json", []) or []
    details = read_json(ROOT / "stage1_details.json", []) or []
    metadata = read_json(ROOT / "stage1_metadata.json", {}) or {}
    universe_meta = read_json(ROOT / "universe.metadata.json", {}) or {}
    detail_map = {symbol(row): row for row in details if symbol(row)}
    source_map = universe_meta.get("sourceBySymbol") or {}
    name_map = universe_meta.get("companyNames") or {}
    load_env()
    api_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if not api_key:
        raise RuntimeError("FMP_KEY_MISSING")
    symbols = [symbol(row) for row in candidates if symbol(row)]
    quotes, quote_errors, measured_at = fresh_measurements(symbols, api_key)
    events, event_error = event_calendar(api_key)
    enrichment, enrichment_errors = screener_enrichment(api_key)
    pipeline_timestamp = datetime.fromtimestamp((ROOT / "candidate_pool.json").stat().st_mtime, timezone.utc).isoformat()
    run_id = str(next((row.get("_system2_run_id") for row in candidates if isinstance(row, dict) and row.get("_system2_run_id")), None) or metadata.get("run_id") or pipeline_timestamp)
    config_hash = hash_files([ROOT / "system2-config.json", ROOT / "b2_stage1_cheap_filter.py", ROOT / "stage1_v2_shadow_router.py"])
    action_map = retained_corporate_actions(set(symbols), pipeline_timestamp[:10], session)
    output: list[dict[str, Any]] = []
    for candidate in candidates:
        key = symbol(candidate)
        if not key:
            continue
        v1 = detail_map.get(key, {})
        quote = quotes.get(key, {})
        profile = enrichment.get(key, {})
        price = number(quote.get("price") or profile.get("price") or v1.get("price"))
        adv = number(quote.get("avgVolume") or quote.get("averageVolume") or profile.get("avgVolume") or profile.get("averageVolume") or v1.get("averageVolume"))
        dollar_adv = price * adv if price is not None and adv is not None else None
        bid, ask = number(quote.get("bid")), number(quote.get("ask"))
        spread = ((ask - bid) / ((ask + bid) / 2) * 100) if bid and ask and ask >= bid else None
        reasons = list(v1.get("rejectReasons") or [])
        recent = bool(v1.get("reentryBlocked") or "recent_shadow_rejection" in reasons)
        event = events.get(key)
        if not event and v1.get("earningsBlackoutNext5d"):
            event = {"date": v1.get("earningsDate"), "source": "production_stage1_retained_earnings_calendar"}
        event_type = "EARNINGS" if event else ("BINARY_EVENT" if candidate.get("binary_event_risk") else None)
        if event_type == "BINARY_EVENT":
            event = {"date": candidate.get("event_date"), "source": candidate.get("event_source")}
        actions = action_map.get(key, [])
        action = {"state": "CORPORATE_ACTION_UNRESOLVED" if actions else "NO_RETAINED_ACTION_FOUND", "actions": actions}
        # The universe builder is the authoritative structural identity screen for
        # rows V1 did not profile because of its recent-shadow early exit.
        identity_state = "FRESH_SCREENER_VALIDATED" if profile else ("PROFILE_VALIDATED" if v1.get("companyName") else ("UPSTREAM_UNIVERSE_STRUCTURAL_VALIDATED" if key in name_map else "UNRESOLVED"))
        source_failure = price in (None, 0) or adv in (None, 0) or bool(quote_errors and not quotes and enrichment_errors and not enrichment)
        profile_structural = []
        if profile.get("isEtf") or profile.get("isFund"):
            profile_structural.append("fund_or_etf")
        if profile.get("isActivelyTrading") is False:
            profile_structural.append("inactive")
        classification, v2_reasons, flags = classify({
            "v1_reasons": reasons + profile_structural, "recent_shadow": recent, "price": price, "adv": adv,
            "dollar_adv": dollar_adv, "source_failure": source_failure, "blocked": key in BLOCKED_TICKERS,
            "identity_unresolved": identity_state == "UNRESOLVED", "corporate_action_unresolved": action["state"] == "CORPORATE_ACTION_UNRESOLVED",
            "event": event_type,
        })
        atr = number(v1.get("atrPct") or candidate.get("atrPct") or candidate.get("atr5MinPct"))
        output.append({
            "symbol": key, "run_id": run_id, "trading_session": session, "next_open_timestamp": timing["next_session_open"],
            "pipeline_timestamp": pipeline_timestamp, "measurement_timestamp": measured_at,
            "production_stage1_v1_result": v1.get("status") or "UNKNOWN",
            "production_stage1_v1_rejection_reasons": reasons,
            "stage1_v2_classification": classification, "stage1_v2_reasons": v2_reasons,
            "stage1_v2_context_flags": flags, "recent_shadow_context": recent,
            "recent_shadow_metadata": v1.get("reentryBlockDetail"),
            "event_route_state": "ROUTED" if classification == "EVENT_ROUTE" else "NOT_ROUTED",
            "event_type": event_type, "event_date": (event or {}).get("date"),
            "event_timestamp": (event or {}).get("timestamp") or (event or {}).get("date"),
            "event_source": (event or {}).get("source") or ("FMP stable/earnings-calendar" if event_type == "EARNINGS" else None),
            "event_quality_state": "POINT_IN_TIME_CALENDAR" if event_type else "NO_RECOGNIZED_EVENT",
            "price": price, "average_share_volume": adv, "dollar_adv": dollar_adv,
            "bid": bid, "ask": ask, "spread_pct": spread, "atr_or_volatility_pct": atr,
            "requested_notional": None,
            "capacity_025pct": dollar_adv * .0025 if dollar_adv is not None else None,
            "capacity_050pct": dollar_adv * .005 if dollar_adv is not None else None,
            "capacity_100pct": dollar_adv * .01 if dollar_adv is not None else None,
            "sector": profile.get("sector") or v1.get("sector") or candidate.get("sector"),
            "market_cap": number(profile.get("marketCap") or v1.get("marketCap") or quote.get("marketCap") or candidate.get("marketCap")),
            "company_name": profile.get("companyName") or v1.get("companyName") or name_map.get(key) or quote.get("name"),
            "instrument_identity_state": identity_state, "corporate_action_state": action,
            "data_quality_state": "COMPLETE_CORE" if not source_failure and price and adv else "CRITICAL_MISSING",
            "data_quality_details": {"quote_provider": "FMP stable/batch-quote", "adv_provider": "FMP stable/company-screener", "quote_errors": quote_errors, "enrichment_errors": enrichment_errors, "event_error": event_error},
            "source_lineage": {"universe_source": source_map.get(key), "candidate_source": candidate.get("source"), "intake_source_layer": v1.get("intake_source_layer") or candidate.get("intake_source_layer"), "candidate_metadata": candidate},
            "config_hash": config_hash, "research_only": True, "non_trading": True,
        })
    counts = dict(Counter(row["stage1_v2_classification"] for row in output))
    production = {"input": len(candidates), "pass": sum(row["production_stage1_v1_result"] == "PASS" for row in output), "reject": sum(row["production_stage1_v1_result"] == "REJECT" for row in output)}
    directory = run_directory(session, run_id)
    payload = {"schema_version": 1, "research_only": True, "non_trading": True, "immutable_membership": True, **timing,
               "run_id": run_id, "pipeline_timestamp": pipeline_timestamp, "config_hash": config_hash,
               "production_stage1_counts": production, "counts": counts, "quote_errors": quote_errors, "enrichment_errors": enrichment_errors, "event_error": event_error, "rows": output}
    shadow_path = write_immutable(directory / "stage1_v2_shadow.json", payload)
    cohort_rows: list[dict[str, Any]] = []
    mapping = {"ALPHA_ELIGIBLE": COHORTS[0], "ALPHA_ELIGIBLE_CAPACITY_LIMITED": COHORTS[1], "EVENT_ROUTE": COHORTS[2]}
    for row in output:
        if row["stage1_v2_classification"] in mapping:
            cohort_rows.append({**row, "cohort": mapping[row["stage1_v2_classification"]], "trading_date": session, "entry_source": "NEXT_REGULAR_SESSION_OPEN"})
        if row["production_stage1_v1_result"] == "PASS":
            cohort_rows.append({**row, "cohort": COHORTS[3], "trading_date": session, "entry_source": "NEXT_REGULAR_SESSION_OPEN"})
    cohort_path = write_immutable(directory / "stage1_v2_cohort_membership.json", {"schema_version": 1, "research_only": True, "non_trading": True, "immutable_membership": True, **timing, "run_id": run_id, "source_artifact": str(shadow_path), "rows": cohort_rows})
    return {"ok": True, "path": str(shadow_path), "cohort_path": str(cohort_path), "counts": counts, "production": production,
            "capacity_limited": counts.get("ALPHA_ELIGIBLE_CAPACITY_LIMITED", 0), "recent_shadow_measured": sum(row["recent_shadow_context"] and row["data_quality_state"] == "COMPLETE_CORE" for row in output),
            "recent_shadow_total": sum(row["recent_shadow_context"] for row in output), "broker_calls": 0}


def update() -> dict[str, Any]:
    artifacts = sorted(RESEARCH_ROOT.glob("*/*/stage1_v2_cohort_membership.json"))
    source_rows: list[tuple[Path, dict[str, Any]]] = []
    for path in artifacts:
        for row in (read_json(path, {}) or {}).get("rows", []):
            source_rows.append((path, row))
    # Bound memory: the canonical EOD cache is multi-gigabyte. Resolve members
    # in small symbol groups while retaining the same V2 authority and calendar.
    rows: list[dict[str, Any]] = []
    ordered_symbols = sorted({row["symbol"] for _, row in source_rows})
    action_index = retained_corporate_actions(set(ordered_symbols), "2000-01-01", "2035-12-31")
    for start in range(0, len(ordered_symbols), 50):
        group_symbols = set(ordered_symbols[start:start + 50])
        resolver = ResearchPriceResolver(group_symbols | {"SPY"} | set(SECTOR_ETFS.values()))
        def cached_action_state(item: str, first: str, last: str) -> dict[str, Any]:
            actions = [action for action in action_index.get(item, []) if first <= action["date"] <= last]
            return {"state": "CORPORATE_ACTION_UNRESOLVED" if actions else "NO_RETAINED_ACTION_FOUND", "actions": actions}
        resolver.corporate_action_state = cached_action_state  # type: ignore[method-assign]
        rows.extend({**row, "membership_artifact": str(path), **label(row, resolver)} for path, row in source_rows if row["symbol"] in group_symbols)
    now, directory = utc_now(), RESEARCH_ROOT / "scoreboards"
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    outcomes = write_immutable(directory / f"stage1_v2_outcomes_{stamp}.json", {"schema_version": 2, "research_only": True, "non_trading": True, "created_at": now.isoformat(), "rows": rows})
    scores = []
    for cohort in COHORTS:
        group = [row for row in rows if row["cohort"] == cohort]
        record: dict[str, Any] = {"cohort": cohort, "n": len(group), "unique_dates": len({row["trading_date"] for row in group})}
        states = [row.get("outcome_state") for row in group]
        record["missing_pct"] = round(100 * sum(state == "MISSING_PRICE" for state in states) / len(group), 4) if group else None
        for horizon in HORIZONS:
            available = [row[f"d{horizon}"] for row in group if (row.get(f"d{horizon}") or {}).get("state") == "AVAILABLE"]
            for field, suffix in (("raw_return_pct", "raw"), ("spy_adjusted_return_pct", "spy_adjusted"), ("sector_adjusted_return_pct", "sector_adjusted")):
                values = [item[field] for item in available if item.get(field) is not None]
                record[f"d{horizon}_{suffix}_mean"] = statistics.fmean(values) if values else None
                record[f"d{horizon}_{suffix}_median"] = statistics.median(values) if values else None
            raw = [item["raw_return_pct"] for item in available if item.get("raw_return_pct") is not None]
            record[f"d{horizon}_win_rate_pct"] = 100 * sum(value > 0 for value in raw) / len(raw) if raw else None
        record["evidence_state"] = "PRELIMINARY_CHECK_ONLY" if record["unique_dates"] >= 30 else "TOO_THIN_NOT_A_VERDICT"
        scores.append(record)
    scoreboard = write_immutable(directory / f"stage1_v2_scoreboard_{stamp}.json", {"schema_version": 2, "research_only": True, "non_trading": True, "created_at": now.isoformat(), "minimum_dates_for_preliminary": 30, "cohorts": scores})
    return {"ok": True, "memberships": len(artifacts), "rows": len(rows), "outcomes": str(outcomes), "scoreboard": str(scoreboard), "broker_calls": 0}


def self_test() -> dict[str, Any]:
    cases = {
        "capacity": classify({"v1_reasons": ["avg_volume_below_1m"], "price": 20, "adv": 750000, "dollar_adv": 15_000_000}),
        "event": classify({"v1_reasons": ["earnings_blackout_next_5d"], "price": 20, "adv": 2_000_000, "dollar_adv": 40_000_000, "event": "EARNINGS"}),
        "recent": classify({"v1_reasons": ["recent_shadow_rejection"], "recent_shadow": True, "price": 20, "adv": 2_000_000, "dollar_adv": 40_000_000}),
        "structural": classify({"v1_reasons": ["fund_or_etf"], "price": 20, "adv": 2_000_000, "dollar_adv": 40_000_000}),
        "quality": classify({"v1_reasons": [], "price": None, "adv": None, "dollar_adv": None}),
    }
    expected = {"capacity": "ALPHA_ELIGIBLE_CAPACITY_LIMITED", "event": "EVENT_ROUTE", "recent": "ALPHA_ELIGIBLE", "structural": "REJECT_STRUCTURAL", "quality": "REJECT_DATA_QUALITY"}
    assertions = {name: cases[name][0] == state for name, state in expected.items()}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "immutable.json"
        write_immutable(path, {"v1": "PASS", "v2": "ALPHA_ELIGIBLE", "research_only": True})
        stored = read_json(path, {})
        assertions["v1_v2_side_by_side"] = stored.get("v1") == "PASS" and stored.get("v2") == "ALPHA_ELIGIBLE"
        try:
            write_immutable(path, {"overwrite": True})
            assertions["immutable_replay_does_not_overwrite"] = False
        except FileExistsError:
            assertions["immutable_replay_does_not_overwrite"] = True
    assertions["no_broker_module_loaded"] = not any("alpaca" in name.lower() or "broker" in name.lower() for name in sys.modules)
    ok = all(assertions.values())
    return {"ok": ok, "assertions": assertions, "cases": {name: result[0] for name, result in cases.items()}, "broker_calls": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("create", "update", "self-test"))
    args = parser.parse_args()
    result = {"create": create, "update": update, "self-test": self_test}[args.command]()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
