#!/usr/bin/env python3
"""Best-effort immutable provenance hooks; never participates in selection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_telemetry_common import next_market_session, run_directory, write_immutable


def write_universe_provenance(layers: list[dict[str, Any]], universe: list[str], assigned: dict[str, str], target: int) -> Path:
    timing = next_market_session()
    directory = run_directory(timing["trading_session"])
    seen: set[str] = set()
    decisions = []
    lineage: dict[str, dict[str, Any]] = {}
    admitted = 0
    for layer in layers:
        clean_symbols = [str(r.get("symbol") or "").upper() for r in layer.get("cleaned", []) if r.get("symbol")]
        clean_set = set(clean_symbols)
        raw_symbols = [str((r.get("symbol") or r.get("ticker") or "")).upper() for r in layer.get("rows", []) if isinstance(r, dict)]
        for symbol in raw_symbols:
            if symbol and symbol not in clean_set:
                decisions.append({"ticker": symbol, "source": layer["label"], "status": "REJECTED", "rejection_reason": "INVALID_OR_QUALITY_GATE", "duplicate": False, "cap_full": False})
        for symbol in clean_symbols:
            duplicate = symbol in seen
            cap_full = admitted >= target and not duplicate
            status = "DUPLICATE" if duplicate else "CAP" if cap_full else "ADMITTED"
            decisions.append({"ticker": symbol, "source": layer["label"], "status": status, "rejection_reason": status if status != "ADMITTED" else None, "duplicate": duplicate, "cap_full": cap_full})
            entry = lineage.setdefault(symbol, {"sources_present": [], "primary_assigned_source": assigned.get(symbol), "base_source": None, "candidate_overlay_sources": []})
            if layer["label"] not in entry["sources_present"]:
                entry["sources_present"].append(layer["label"])
            if entry["base_source"] is None:
                entry["base_source"] = layer["label"]
            if not duplicate and not cap_full:
                seen.add(symbol); admitted += 1
    payload = {"schema_version": 1, "research_only": True, "non_trading": True, "run_id": directory.name,
               **timing, "created_at": datetime.now(timezone.utc).isoformat(), "target_cap": target,
               "final_universe_count": len(universe), "provider_requests": [{"source": x["label"], "provider": "FMP", "request_started_at": x.get("request_started_at"), "request_completed_at": x.get("request_completed_at"), "provider_as_of": x.get("provider_as_of"), "raw_symbols": [str((r.get("symbol") or r.get("ticker") or "")).upper() for r in x.get("rows", []) if isinstance(r, dict)], "clean_symbols": [r.get("symbol") for r in x.get("cleaned", [])]} for x in layers],
               "decisions": decisions, "source_lineage": lineage}
    return write_immutable(directory / "universe_provenance.json", payload)


def write_catalyst_pre_cap(records: list[dict[str, Any]], selected: list[dict[str, Any]], base_universe: set[str], limit: int) -> Path:
    timing = next_market_session(); directory = run_directory(timing["trading_session"])
    ordered = sorted(records, key=lambda r: (r.get("catalyst_score", 0), r.get("catalyst_datetime", "")), reverse=True)
    selected_symbols = {str(r.get("symbol") or "").upper() for r in selected}
    first_seen: set[str] = set(); rows = []
    for rank, row in enumerate(ordered, 1):
        ticker = str(row.get("symbol") or row.get("ticker") or "").upper(); duplicate = ticker in first_seen
        if ticker: first_seen.add(ticker)
        chosen = ticker in selected_symbols and not duplicate
        reason = None if chosen else "DUPLICATE" if duplicate else "CAP" if ticker else "INVALID"
        rows.append({"ticker": ticker, "source": row.get("source"), "event_type": row.get("sub_type"), "event_timestamp": row.get("catalyst_datetime"), "system_observed_timestamp": datetime.now(timezone.utc).isoformat(), "raw_rank_inputs": {"catalyst_score": row.get("catalyst_score"), "catalyst_datetime": row.get("catalyst_datetime")}, "rank": rank, "duplicate": duplicate, "inside_base_universe": ticker in base_universe, "selected_top_30": chosen, "exclusion_reason": reason})
    payload = {"schema_version": 1, "research_only": True, "non_trading": True, "run_id": directory.name, **timing,
               "created_at": datetime.now(timezone.utc).isoformat(), "configured_limit": limit, "candidates": rows,
               "scope_note": "Collector-invalid/stale raw provider rows are not candidate objects and remain unavailable; no historical reconstruction is performed."}
    return write_immutable(directory / "catalyst_pre_cap.json", payload)
