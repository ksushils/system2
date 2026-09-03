#!/usr/bin/env python3
"""Prospective, immutable, non-trading swing champion/control telemetry."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import random
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from research_telemetry_common import NY, RESEARCH_ROOT, is_market_session, next_market_session, read_json, run_directory, utc_now, write_immutable

ROOT = Path(__file__).resolve().parent
HORIZONS = (1, 2, 3, 5, 7)
COHORTS = (
    "CLUSTER_KEPT_NEXT_OPEN_V1",
    "CLUSTER_KEPT_PREMARKET_CONTEXT_V1",
    "STAGE2_REJECTED_BY_CLUSTER_V1",
    "STAGE1_RANDOM_MATCHED_V1",
)
CHALLENGERS = ("UNCHASED_SWING_RANK_V1", "LOW_EXTENSION_BASELINE_V1")
SECTOR_ETFS = {"Technology":"XLK","Communication Services":"XLC","Consumer Cyclical":"XLY","Consumer Defensive":"XLP","Financial Services":"XLF","Healthcare":"XLV","Industrials":"XLI","Energy":"XLE","Utilities":"XLU","Real Estate":"XLRE","Basic Materials":"XLB"}


def ticker(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()


def number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def file_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes() if path.exists() else b"MISSING")
    return digest.hexdigest()


def latest_artifact(session: str, name: str) -> Path | None:
    found = sorted((RESEARCH_ROOT / session).glob(f"*/{name}"))
    return found[-1] if found else None


def percentile_map(rows: list[dict[str, Any]], field: str) -> dict[str, float]:
    """Cross-sectional percentile ranks with average ranks for ties."""
    pairs = sorted((number(row.get(field)), ticker(row)) for row in rows if number(row.get(field)) is not None)
    result: dict[str, float] = {}
    if not pairs:
        return result
    denominator = max(1, len(pairs) - 1)
    index = 0
    while index < len(pairs):
        end = index + 1
        while end < len(pairs) and pairs[end][0] == pairs[index][0]:
            end += 1
        average_rank = (index + end - 1) / 2
        percentile = 100 * average_rank / denominator
        for _, symbol in pairs[index:end]:
            result[symbol] = round(percentile, 6)
        index = end
    return result


def assign_ranks(rows: list[dict[str, Any]], challenger: str) -> None:
    valid = [row for row in rows if row.get("challenger") == challenger and row.get("final_score") is not None]
    valid.sort(key=lambda row: (-row["final_score"], row["symbol"]))
    population = len([row for row in rows if row.get("challenger") == challenger])
    for index, row in enumerate(valid, 1):
        row["rank"] = index
        row["quintile"] = min(5, ((index - 1) * 5 // max(1, len(valid))) + 1)
        row["scored_population_size"] = len(valid)
        row["population_size"] = population


def create_challengers() -> dict[str, Any]:
    timing = next_market_session(); session = timing["trading_session"]
    existing = latest_artifact(session, "unchased_challenger_rankings.json")
    if existing:
        return {"ok": True, "idempotent": True, "path": str(existing)}
    scored = read_json(ROOT / "stage2_surgical_strike_scored.json", []) or []
    eligible = [row for row in scored if row.get("status") == "OK" and ticker(row)]
    normalized = []
    for row in eligible:
        raw_extension = number(row.get("distanceFromVWAP") if row.get("distanceFromVWAP") is not None else row.get("distanceFromVWMA"))
        normalized.append({**row, "_atr": number(row.get("atrPct") if row.get("atrPct") is not None else row.get("atr5MinPct")),
                           "_atr_source": "atrPct" if row.get("atrPct") is not None else "atr5MinPct" if row.get("atr5MinPct") is not None else None,
                           "_extension": max(0.0, raw_extension) if raw_extension is not None else None,
                           "_raw_extension": raw_extension, "_rs": number(row.get("rsVsSpy")), "_sector": number(row.get("sectorAlpha"))})
    percentiles = {field: percentile_map(normalized, field) for field in ("_atr", "_extension", "_rs", "_sector")}
    pipeline_timestamp = datetime.fromtimestamp((ROOT / "stage2_surgical_strike_scored.json").stat().st_mtime, timezone.utc).isoformat()
    config_hash = file_hash([ROOT / "system2-config.json", ROOT / "b3_surgical_strike_stage2.py"])
    output = []
    for row in normalized:
        symbol = ticker(row)
        raw = {"atr_pct": row["_atr"], "atr_source": row["_atr_source"], "extension_pct_raw": row["_raw_extension"],
               "positive_extension_pct": row["_extension"], "rs_vs_spy_pct": row["_rs"], "sector_alpha_pct": row["_sector"]}
        pct = {"atr": percentiles["_atr"].get(symbol), "extension": percentiles["_extension"].get(symbol),
               "rs": percentiles["_rs"].get(symbol), "sector": percentiles["_sector"].get(symbol)}
        signed = {name: round(100-value, 6) if value is not None else None for name, value in pct.items()}
        available = [value for value in signed.values() if value is not None]
        score = sum(available) / len(available) if len(available) >= 3 else None
        missing = [name for name, value in signed.items() if value is None]
        output.append({"challenger": CHALLENGERS[0], "symbol": symbol, "trading_date": session, "next_open_timestamp": timing["next_session_open"],
                       "sector": row.get("sector"),
                       "current_stage2_score": number(row.get("setupQualityScore") or row.get("setup_score")),
                       "entry_source": "NEXT_REGULAR_SESSION_OPEN", "pipeline_timestamp": pipeline_timestamp, "config_hash": config_hash,
                       "raw_fields": raw, "percentiles": pct, "signed_components": signed, "missing_components": missing,
                       "missing_component_count": len(missing), "score_state": "AVAILABLE" if score is not None else "UNCHASED_SCORE_MISSING",
                       "final_score": round(score, 6) if score is not None else None, "rank": None, "quintile": None, "population_size": len(eligible)})
        simple_values = [signed["atr"], signed["extension"]]
        simple_score = sum(simple_values) / 2 if all(value is not None for value in simple_values) else None
        simple_missing = [name for name in ("atr", "extension") if signed[name] is None]
        output.append({"challenger": CHALLENGERS[1], "symbol": symbol, "trading_date": session, "next_open_timestamp": timing["next_session_open"],
                       "sector": row.get("sector"),
                       "current_stage2_score": number(row.get("setupQualityScore") or row.get("setup_score")),
                       "entry_source": "NEXT_REGULAR_SESSION_OPEN", "pipeline_timestamp": pipeline_timestamp, "config_hash": config_hash,
                       "raw_fields": {"atr_pct": raw["atr_pct"], "atr_source": raw["atr_source"], "extension_pct_raw": raw["extension_pct_raw"], "positive_extension_pct": raw["positive_extension_pct"]},
                       "percentiles": {"atr": pct["atr"], "extension": pct["extension"]}, "signed_components": {"atr": signed["atr"], "extension": signed["extension"]},
                       "missing_components": simple_missing, "missing_component_count": len(simple_missing),
                       "score_state": "AVAILABLE" if simple_score is not None else "LOW_EXTENSION_SCORE_MISSING",
                       "final_score": round(simple_score, 6) if simple_score is not None else None, "rank": None, "quintile": None, "population_size": len(eligible)})
    for challenger in CHALLENGERS:
        assign_ranks(output, challenger)
    directory = run_directory(session)
    payload = {"schema_version": 1, "research_only": True, "non_trading": True, "immutable_rankings": True, **timing,
               "pipeline_timestamp": pipeline_timestamp, "config_hash": config_hash, "definitions": {
                   CHALLENGERS[0]: "equal mean of available inverse ATR/positive-extension/RS/sector percentiles; minimum 3 of 4",
                   CHALLENGERS[1]: "equal mean of inverse ATR and positive-extension percentiles; both required"},
               "population_size": len(eligible), "rows": output}
    path = write_immutable(directory / "unchased_challenger_rankings.json", payload)
    return {"ok": True, "path": str(path), "population": len(eligible), "rows": len(output),
            "scored": {name: sum(r["challenger"] == name and r["final_score"] is not None for r in output) for name in CHALLENGERS}}


def row_fields(row: dict[str, Any], state: str, lineage: dict[str, Any]) -> dict[str, Any]:
    cluster = row.get("cluster") or {}
    sector = row.get("sector") or cluster.get("sector")
    return {
        "symbol": ticker(row), "entry_source": "NEXT_REGULAR_SESSION_OPEN", "stage2_score": number(row.get("setupQualityScore") or row.get("setup_score")),
        "cluster_id": f"{cluster.get('sector') or row.get('sector') or 'Unknown'}|{cluster.get('etf') or SECTOR_ETFS.get(row.get('sector'),'UNKNOWN')}",
        "cluster_state": state, "cluster_rank": cluster.get("clusterRank"), "cluster_size": cluster.get("clusterSizeBefore"),
        "sector": sector, "atr_pct": number(row.get("atrPct") or row.get("atr5MinPct")),
        "market_cap": number(row.get("marketCap")), "average_volume": number(row.get("averageVolume") or row.get("avg_volume_20d")),
        "dollar_volume": number(row.get("dollarVolume")), "source_lineage": lineage.get(ticker(row), {}),
        "data_quality_flags": (["MISSING_SECTOR"] if not sector else []) + (["MISSING_SOURCE_LINEAGE"] if ticker(row) not in lineage else []),
    }


def create_membership() -> dict[str, Any]:
    timing = next_market_session(); session = timing["trading_session"]
    existing = latest_artifact(session, "swing_cohort_membership.json")
    if existing:
        return {"ok": True, "idempotent": True, "path": str(existing)}
    kept = read_json(ROOT / "stage7_clustered_survivors.json", []) or []
    rejected_all = read_json(ROOT / "stage7_cluster_rejections.json", []) or []
    rejected = [r for r in rejected_all if str(r.get("clusterRejectReason") or "").startswith("cluster_cap_")]
    stage1 = read_json(ROOT / "stage1_survivors.json", []) or []
    provenance_path = latest_artifact(session, "funnel_membership.json")
    provenance = read_json(provenance_path or Path("/missing"), {}) or {}
    lineage = provenance.get("source_lineage", {})
    seed_text = f"{session}|STAGE1_RANDOM_MATCHED_V1"
    seed = int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16)
    excluded = {ticker(r) for r in kept + rejected}
    eligible = sorted([r for r in stage1 if ticker(r) and ticker(r) not in excluded], key=ticker)
    control = random.Random(seed).sample(eligible, min(len(kept), len(eligible)))
    pipeline_stamp = datetime.fromtimestamp((ROOT / "stage7_clustered_survivors.json").stat().st_mtime, timezone.utc).isoformat()
    config_hash = file_hash([ROOT / "system2-config.json", ROOT / "b3_surgical_strike_stage2.py", ROOT / "b4_correlation_cluster_engine.py"])
    rows = []
    for cohort, source, state in (
        (COHORTS[0], kept, "KEPT"), (COHORTS[1], kept, "KEPT"),
        (COHORTS[2], rejected, "REJECTED"), (COHORTS[3], control, "CONTROL"),
    ):
        for row in source:
            rows.append({"cohort": cohort, "membership_timestamp": utc_now().isoformat(), "pipeline_run_id": provenance.get("run_id") or pipeline_stamp,
                         "config_hash": config_hash, "trading_date": session, "next_open_timestamp": timing["next_session_open"],
                         **row_fields(row, state, lineage), "outcome_state": "PENDING"})
    directory = run_directory(session)
    payload = {"schema_version": 1, "research_only": True, "non_trading": True, "immutable_membership": True, **timing,
               "pipeline_completed_at": pipeline_stamp, "deterministic_control_seed": seed, "cohort_counts": {c: sum(r["cohort"] == c for r in rows) for c in COHORTS}, "rows": rows}
    path = write_immutable(directory / "swing_cohort_membership.json", payload)
    return {"ok": True, "path": str(path), "counts": payload["cohort_counts"], "seed": seed}


def load_price_series(symbols: set[str]) -> dict[str, list[dict[str, Any]]]:
    chosen: dict[str, str] = {}
    for path in glob.glob(str(ROOT / "data/fmp_cache/*/*historical-price-eod*json")):
        match = re.search(r"symbol[=_]([A-Za-z0-9.^-]+)", Path(path).name)
        symbol = match.group(1).upper() if match else ""
        if symbol in symbols and (symbol not in chosen or path > chosen[symbol]):
            chosen[symbol] = path
    output = {}
    for symbol, path in chosen.items():
        data = read_json(Path(path), {})
        rows = data.get("data", []) if isinstance(data, dict) else data
        output[symbol] = sorted([r for r in rows if isinstance(r, dict) and r.get("date")], key=lambda r: str(r["date"]))
    for path in sorted((RESEARCH_ROOT / "daily_marks").glob("challenger_daily_mark_*.json")):
        payload = read_json(path, {}) or {}
        for row in payload.get("rows", []):
            symbol = ticker(row)
            if symbol not in symbols or not row.get("date") or not number(row.get("open")) or not number(row.get("close")):
                continue
            by_date = {str(bar["date"])[:10]: bar for bar in output.get(symbol, [])}
            by_date[str(row["date"])[:10]] = row
            output[symbol] = [bar for _, bar in sorted(by_date.items())]
    return output


def capture_daily_marks(symbols: set[str]) -> dict[str, Any]:
    now = utc_now(); market_date = now.astimezone(NY).date()
    while not is_market_session(market_date): market_date -= timedelta(days=1)
    path = RESEARCH_ROOT / "daily_marks" / f"challenger_daily_mark_{market_date.isoformat()}.json"
    if path.exists(): return {"ok":True,"idempotent":True,"path":str(path)}
    rows=[]; error=None
    try:
        from prospective_research_telemetry import get_json, load_dotenv
        import os
        load_dotenv(); key=os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
        if not key: raise RuntimeError("FMP_KEY_MISSING")
        ordered=sorted(symbols)
        for start in range(0,len(ordered),75):
            raw=get_json("batch-quote?symbols=" + ",".join(ordered[start:start+75]),key)
            for quote in raw if isinstance(raw,list) else []:
                rows.append({"symbol":ticker(quote),"date":market_date.isoformat(),"open":number(quote.get("open")),
                             "high":number(quote.get("dayHigh") or quote.get("high")),"low":number(quote.get("dayLow") or quote.get("low")),
                             "close":number(quote.get("price")),"provider_timestamp":quote.get("timestamp"),"quality":"FRESH"})
    except Exception as exc:
        error=f"{type(exc).__name__}:{exc}"
    write_immutable(path,{"schema_version":1,"research_only":True,"non_trading":True,"created_at":now.isoformat(),"market_date":market_date.isoformat(),
                          "provider":"FMP batch-quote","source_error":error,"requested_symbols":len(symbols),"returned_rows":len(rows),"rows":rows})
    return {"ok":error is None,"path":str(path),"rows":len(rows),"source_error":error}


def label(row: dict[str, Any], series: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    symbol = row["symbol"]; trading_date = row["trading_date"]; bars = series.get(symbol)
    if not bars:
        return {"outcome_state": "MISSING_PRICE", "missing_reason": "SYMBOL_ABSENT_FROM_LOCAL_POINT_IN_TIME_CACHE"}
    index = next((i for i, bar in enumerate(bars) if str(bar["date"])[:10] == trading_date), None)
    if index is None:
        return {"outcome_state": "MISSING_PRICE", "missing_reason": "ENTRY_SESSION_NOT_IN_CACHE"}
    entry = number(bars[index].get("open"))
    if not entry:
        return {"outcome_state": "MISSING_PRICE", "missing_reason": "ENTRY_OPEN_MISSING"}
    result: dict[str, Any] = {"entry_open": entry, "entry_open_timestamp": row["next_open_timestamp"], "entry_source": "FMP_EOD_OPEN", "outcome_state": "PARTIAL"}
    spy = series.get("SPY", []); si = next((i for i,b in enumerate(spy) if str(b["date"])[:10] == trading_date), None)
    sector_symbol = SECTOR_ETFS.get(row.get("sector")); sector = series.get(sector_symbol, []) if sector_symbol else []; xi = next((i for i,b in enumerate(sector) if str(b["date"])[:10] == trading_date), None)
    for horizon in HORIZONS:
        key = f"d{horizon}"
        if index + horizon >= len(bars):
            result[key] = {"state": "MISSING_PRICE", "reason": "FORWARD_HORIZON_NOT_YET_AVAILABLE"}; continue
        close = number(bars[index+horizon].get("close")); raw = (close / entry - 1) * 100 if close else None
        spy_return = None
        if si is not None and si+horizon < len(spy):
            so=number(spy[si].get("open")); sc=number(spy[si+horizon].get("close")); spy_return=(sc/so-1)*100 if so and sc else None
        sector_return = None
        if xi is not None and xi+horizon < len(sector):
            xo=number(sector[xi].get("open")); xc=number(sector[xi+horizon].get("close")); sector_return=(xc/xo-1)*100 if xo and xc else None
        result[key] = {"state": "AVAILABLE", "close": close, "raw_return_pct": raw, "spy_return_pct": spy_return,
                       "spy_adjusted_return_pct": raw-spy_return if raw is not None and spy_return is not None else None,
                       "sector_return_pct": sector_return, "sector_adjusted_return_pct": raw-sector_return if raw is not None and sector_return is not None else None}
    through = bars[index:min(index+8,len(bars))]
    highs=[number(x.get("high")) for x in through]; lows=[number(x.get("low")) for x in through]
    result["mfe_pct"] = (max(x for x in highs if x is not None)/entry-1)*100 if any(x is not None for x in highs) else None
    result["mae_pct"] = (min(x for x in lows if x is not None)/entry-1)*100 if any(x is not None for x in lows) else None
    if all(result[f"d{h}"]["state"] == "AVAILABLE" for h in HORIZONS): result["outcome_state"] = "COMPLETE"
    return result


def update_outcomes() -> dict[str, Any]:
    memberships = sorted(RESEARCH_ROOT.glob("*/*/swing_cohort_membership.json"))
    rows=[]
    for path in memberships:
        payload=read_json(path,{}) or {}
        for row in payload.get("rows",[]): rows.append((path,row))
    symbols={r["symbol"] for _,r in rows}|{"SPY"}|set(SECTOR_ETFS.values()); series=load_price_series(symbols)
    labelled=[{**row,"membership_artifact":str(path),**label(row,series)} for path,row in rows]
    now=utc_now(); directory=RESEARCH_ROOT/"scoreboards"; stamp=now.strftime("%Y%m%dT%H%M%SZ")
    snapshot={"schema_version":1,"research_only":True,"non_trading":True,"created_at":now.isoformat(),"rows":labelled}
    outpath=write_immutable(directory/f"swing_outcomes_{stamp}.json",snapshot)
    scores=[]
    for cohort in COHORTS:
        group=[r for r in labelled if r["cohort"]==cohort]; record={"cohort":cohort,"n":len(group),"unique_dates":len({r['trading_date'] for r in group}),"missing_pct":round(100*sum(r['outcome_state']=='MISSING_PRICE' for r in group)/len(group),2) if group else None}
        for h in (2,3,5,7):
            available=[r[f"d{h}"] for r in group if isinstance(r.get(f"d{h}"),dict) and r[f"d{h}"].get("state")=="AVAILABLE"]
            for field,name in (("raw_return_pct",f"d{h}_raw"),("spy_adjusted_return_pct",f"d{h}_spy_adjusted"),("sector_adjusted_return_pct",f"d{h}_sector_adjusted")):
                vals=[x[field] for x in available if x.get(field) is not None]; record[name]=sum(vals)/len(vals) if vals else None
            vals=[x["raw_return_pct"] for x in available if x.get("raw_return_pct") is not None]; record[f"d{h}_win_rate_pct"]=100*sum(x>0 for x in vals)/len(vals) if vals else None
        record["evidence_state"]="PRELIMINARY_CHECK_ONLY" if record["unique_dates"]>=30 else "TOO_THIN_NOT_A_VERDICT"
        scores.append(record)
    report=write_immutable(directory/f"swing_scoreboard_{stamp}.json",{"schema_version":1,"research_only":True,"non_trading":True,"created_at":now.isoformat(),"minimum_dates_for_preliminary":30,"preferred_dates_for_promotion":60,"cohorts":scores})
    return {"ok":True,"outcomes":str(outpath),"scoreboard":str(report),"memberships":len(memberships),"rows":len(labelled)}


def spearman(rows: list[dict[str, Any]], horizon: int) -> float | None:
    pairs = [(number(row.get("final_score")), number((row.get(f"d{horizon}") or {}).get("spy_adjusted_return_pct"))) for row in rows]
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i]); result = [0.0] * len(values); i = 0
        while i < len(order):
            j = i + 1
            while j < len(order) and values[order[j]] == values[order[i]]: j += 1
            rank = (i + j - 1) / 2 + 1
            for k in order[i:j]: result[k] = rank
            i = j
        return result
    rx, ry = ranks([x for x, _ in pairs]), ranks([y for _, y in pairs]); mx, my = statistics.mean(rx), statistics.mean(ry)
    denominator = sum((x-mx)**2 for x in rx) ** .5 * sum((y-my)**2 for y in ry) ** .5
    return sum((x-mx)*(y-my) for x,y in zip(rx,ry)) / denominator if denominator else None


def update_challengers() -> dict[str, Any]:
    artifacts = sorted(RESEARCH_ROOT.glob("*/*/unchased_challenger_rankings.json")); source_rows = []
    for path in artifacts:
        payload = read_json(path, {}) or {}
        for row in payload.get("rows", []): source_rows.append((path, row))
    symbols = {row["symbol"] for _, row in source_rows} | {"SPY"} | set(SECTOR_ETFS.values()); mark_result=capture_daily_marks(symbols); series = load_price_series(symbols)
    rows = [{**row, "ranking_artifact": str(path), **label(row, series)} for path, row in source_rows]
    now = utc_now(); stamp = now.strftime("%Y%m%dT%H%M%SZ"); directory = RESEARCH_ROOT / "scoreboards"
    outcomes = write_immutable(directory / f"unchased_challenger_outcomes_{stamp}.json", {"schema_version":1,"research_only":True,"non_trading":True,"created_at":now.isoformat(),"rows":rows})
    report = []
    for challenger in CHALLENGERS:
        challenger_rows = [row for row in rows if row["challenger"] == challenger]
        for quintile in (1,2,3,4,5):
            group = [row for row in challenger_rows if row.get("quintile") == quintile]
            item = {"challenger":challenger,"quintile":f"Q{quintile}","n":len(group),"unique_dates":len({row['trading_date'] for row in group}),
                    "missing_pct":100*sum(row.get("outcome_state")=="MISSING_PRICE" for row in group)/len(group) if group else None}
            for horizon in (3,5,7):
                available = [row[f"d{horizon}"] for row in group if (row.get(f"d{horizon}") or {}).get("state") == "AVAILABLE"]
                raw = [x["raw_return_pct"] for x in available if x.get("raw_return_pct") is not None]
                adjusted = [x["spy_adjusted_return_pct"] for x in available if x.get("spy_adjusted_return_pct") is not None]
                item[f"mean_d{horizon}"] = statistics.mean(raw) if raw else None; item[f"median_d{horizon}"] = statistics.median(raw) if raw else None
                item[f"mean_spy_adjusted_d{horizon}"] = statistics.mean(adjusted) if adjusted else None
                item[f"median_spy_adjusted_d{horizon}"] = statistics.median(adjusted) if adjusted else None
                item[f"win_rate_d{horizon}_pct"] = 100*sum(value>0 for value in raw)/len(raw) if raw else None
            item["evidence_state"] = "PRELIMINARY_CHECK_ONLY" if item["unique_dates"] >= 30 else "TOO_THIN_NOT_A_VERDICT"
            report.append(item)
        missing = [row for row in challenger_rows if row.get("final_score") is None]
        report.append({"challenger":challenger,"quintile":"SCORE_MISSING","n":len(missing),"unique_dates":len({row['trading_date'] for row in missing}),"missing_pct":100.0 if missing else 0.0,"evidence_state":"UNRANKED_RETAINED"})
    correlations = {challenger:{f"d{horizon}":spearman([row for row in rows if row["challenger"]==challenger],horizon) for horizon in (3,5)} for challenger in CHALLENGERS}
    scoreboard = write_immutable(directory / f"unchased_challenger_scoreboard_{stamp}.json", {"schema_version":1,"research_only":True,"non_trading":True,"created_at":now.isoformat(),"minimum_dates_for_verdict":30,"preferred_dates":60,"spearman":correlations,"quintiles":report})
    return {"ok":True,"ranking_artifacts":len(artifacts),"rows":len(rows),"daily_mark":mark_result,"outcomes":str(outcomes),"scoreboard":str(scoreboard)}


def attach_premarket() -> dict[str, Any]:
    timing=next_market_session(); session=timing["trading_session"]; membership_path=latest_artifact(session,"swing_cohort_membership.json")
    if not membership_path:return {"ok":False,"reason":"MISSING_MEMBERSHIP","session":session}
    candidates=sorted((RESEARCH_ROOT/session).glob("*/premarket_0915_*.json")); premarket_path=candidates[-1] if candidates else None
    data=read_json(premarket_path or Path("/missing"),{}) or {}; by={r.get("ticker"):r for r in data.get("rows",[])}
    membership=read_json(membership_path,{}) or {}; rows=[]
    for row in membership.get("rows",[]):
        if row["cohort"] not in COHORTS[:2]:continue
        context=by.get(row["symbol"])
        rows.append({"cohort":row["cohort"],"symbol":row["symbol"],"trading_date":session,"membership_artifact":str(membership_path),
                     "premarket_state":"AVAILABLE" if context else "MISSING_PREMARKET","premarket":context,"missing_reason":None if context else "NO_0915_ROW"})
    path=write_immutable(membership_path.parent/f"swing_premarket_context_{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json",{"schema_version":1,"research_only":True,"non_trading":True,"created_at":utc_now().isoformat(),"source_artifact":str(premarket_path) if premarket_path else None,"rows":rows})
    return {"ok":True,"path":str(path),"rows":len(rows),"missing":sum(r['premarket_state']!='AVAILABLE' for r in rows)}


def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument("command",choices=("create","create-challengers","attach-premarket","update","update-challengers","self-test")); args=parser.parse_args()
    if args.command=="create": result=create_membership()
    elif args.command=="create-challengers": result=create_challengers()
    elif args.command=="attach-premarket": result=attach_premarket()
    elif args.command=="update": result=update_outcomes()
    elif args.command=="update-challengers": result=update_challengers()
    else:
        result={"ok":True,"cohorts":COHORTS,"challengers":CHALLENGERS,"broker_modules_imported":False,"config_hash":file_hash([ROOT/"system2-config.json",ROOT/"b3_surgical_strike_stage2.py",ROOT/"b4_correlation_cluster_engine.py"])}
    print(json.dumps(result,indent=2))


if __name__=="__main__": main()
