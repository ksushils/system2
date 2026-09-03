#!/usr/bin/env python3
"""Point-in-time momentum-level versus premarket-acceleration research only."""

from __future__ import annotations

import argparse
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prospective_research_telemetry import get_json, load_dotenv
from research_telemetry_common import RESEARCH_ROOT, next_market_session, read_json, run_directory, utc_now, write_immutable
from swing_shadow_cohorts import SECTOR_ETFS, capture_daily_marks, file_hash, label, load_price_series, number, ticker

ROOT = Path(__file__).resolve().parent
STATES = ("HIGH_LEVEL_NO_NEW_STRENGTH", "HIGH_LEVEL_ACCELERATING", "LOWER_LEVEL_ACCELERATING", "LOWER_LEVEL_FLAT", "DETERIORATING", "UNKNOWN")
INTERACTIONS = ("NO_NEW_COMPANY_EVENT", "FRESH_POSITIVE_EVENT", "FRESH_NEGATIVE_EVENT", "UNKNOWN")


def latest(session: str, name: str) -> Path | None:
    paths = sorted((RESEARCH_ROOT / session).glob(f"*/{name}")); return paths[-1] if paths else None


def parse_time(value: Any) -> datetime | None:
    if not value: return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")); return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception: return None


def create_nightly() -> dict[str, Any]:
    timing=next_market_session(); session=timing["trading_session"]; existing=latest(session,"momentum_level_nightly.json")
    if existing:return {"ok":True,"idempotent":True,"path":str(existing)}
    rows=[r for r in (read_json(ROOT/"stage2_surgical_strike_scored.json",[]) or []) if r.get("status")=="OK" and ticker(r)]
    pipeline=datetime.fromtimestamp((ROOT/"stage2_surgical_strike_scored.json").stat().st_mtime,timezone.utc).isoformat()
    config_hash=file_hash([ROOT/"system2-config.json",ROOT/"b3_surgical_strike_stage2.py"]); output=[]
    for row in rows:
        extension=number(row.get("distanceFromVWAP") if row.get("distanceFromVWAP") is not None else row.get("distanceFromVWMA"))
        output.append({"symbol":ticker(row),"trading_date":session,"next_open_timestamp":timing["next_session_open"],"pipeline_timestamp":pipeline,"config_hash":config_hash,
                       "nightly_rs_vs_spy_pct":number(row.get("rsVsSpy")),"nightly_sector_alpha_pct":number(row.get("sectorAlpha")),
                       "atr_pct":number(row.get("atrPct") if row.get("atrPct") is not None else row.get("atr5MinPct")),
                       "atr_source":"atrPct" if row.get("atrPct") is not None else "atr5MinPct" if row.get("atr5MinPct") is not None else None,
                       "vwap_vwma_extension_pct":extension,"rvol":number(row.get("volumeRatio")),"price":number(row.get("price")),
                       "price_return_pct":number(row.get("todayReturnPct")),"distance_from_recent_high_pct":number(row.get("pct_from_52wk_high")),
                       "sector":row.get("sector"),"field_states":{"price_return_pct":"FRESH" if row.get("todayReturnPct") is not None else "MISSING"}})
    path=write_immutable(run_directory(session)/"momentum_level_nightly.json",{"schema_version":1,"research_only":True,"non_trading":True,"immutable_membership":True,**timing,"pipeline_timestamp":pipeline,"config_hash":config_hash,"population":len(output),"rows":output})
    return {"ok":True,"path":str(path),"population":len(output)}


def event_symbol(row: dict[str,Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()


def event_time(row: dict[str,Any]) -> datetime | None:
    return parse_time(row.get("publishedDate") or row.get("date") or row.get("timestamp"))


def fetch_events(session: str, pipeline: datetime | None, symbols: set[str], key: str) -> tuple[dict[str,list[dict]],dict[str,Any]]:
    specifications={"news":"news/stock-latest?page=0&limit=250","analyst":"grades-latest?page=0&limit=250","earnings":f"earnings-calendar?from={session}&to={session}"}
    found= {symbol:[] for symbol in symbols}; status={}
    for kind,endpoint in specifications.items():
        try:
            raw=get_json(endpoint,key); raw=raw if isinstance(raw,list) else []; complete=len(raw)<250 if kind!="earnings" else True
            status[kind]={"quality":"FRESH","rows_returned":len(raw),"complete_for_no-event_claim":complete}
            for row in raw:
                symbol=event_symbol(row); when=event_time(row)
                if symbol not in symbols:continue
                if when is None:
                    status[kind]["complete_for_no-event_claim"]=False
                    continue
                if pipeline and when<=pipeline:continue
                polarity="UNKNOWN"
                if kind=="analyst":
                    grade=str(row.get("newGrade") or row.get("gradingAction") or "").lower()
                    if any(x in grade for x in ("buy","outperform","overweight","strong buy")):polarity="POSITIVE"
                    elif any(x in grade for x in ("sell","underperform","underweight")):polarity="NEGATIVE"
                elif kind=="earnings":
                    actual,estimate=number(row.get("epsActual") or row.get("actual")),number(row.get("epsEstimated") or row.get("estimate"))
                    if actual is not None and estimate is not None:polarity="POSITIVE" if actual>estimate else "NEGATIVE" if actual<estimate else "UNKNOWN"
                title=str(row.get("title") or row.get("newsTitle") or "")
                found[symbol].append({"type":kind,"event_timestamp":when.isoformat() if when else None,"polarity":polarity,"title":title[:240],"guidance_flag":any(x in title.lower() for x in ("guidance","outlook","forecast"))})
        except Exception as exc:status[kind]={"quality":"SOURCE_ERROR","error":f"{type(exc).__name__}:{exc}","complete_for_no-event_claim":False}
    return found,status


def classify(high: bool, stock_delta: float | None, sector_delta: float | None) -> str:
    if stock_delta is None or sector_delta is None:return "UNKNOWN"
    if stock_delta<0 and sector_delta<0:return "DETERIORATING"
    accelerating=stock_delta>0 and sector_delta>0
    if high:return "HIGH_LEVEL_ACCELERATING" if accelerating else "HIGH_LEVEL_NO_NEW_STRENGTH"
    return "LOWER_LEVEL_ACCELERATING" if accelerating else "LOWER_LEVEL_FLAT"


def capture_premarket() -> dict[str,Any]:
    timing=next_market_session(); session=timing["trading_session"]; source=latest(session,"momentum_level_nightly.json")
    if not source:return {"ok":False,"reason":"MISSING_NIGHTLY_LEVEL","session":session}
    existing=latest(session,"momentum_acceleration_0915.json")
    if existing:return {"ok":True,"idempotent":True,"path":str(existing)}
    nightly=read_json(source,{}) or {}; rows=nightly.get("rows",[]); symbols={r["symbol"] for r in rows}; market={"SPY","QQQ"}|set(SECTOR_ETFS.values()); quotes={}; source_error=None
    load_dotenv(); key=os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if not key:source_error="FMP_KEY_MISSING"
    else:
        try:
            ordered=sorted(symbols|market)
            for start in range(0,len(ordered),75):
                raw=get_json("batch-quote?symbols="+",".join(ordered[start:start+75]),key)
                for quote in raw if isinstance(raw,list) else []:quotes[ticker(quote)]=quote
        except Exception as exc:source_error=f"{type(exc).__name__}:{exc}"
    pipeline=parse_time(nightly.get("pipeline_timestamp")); events,event_status=fetch_events(session,pipeline,symbols,key) if key else ({s:[] for s in symbols},{k:{"quality":"SOURCE_ERROR","error":"FMP_KEY_MISSING","complete_for_no-event_claim":False} for k in ("news","analyst","earnings")})
    def gap(symbol:str)->float|None:
        q=quotes.get(symbol,{}); price=number(q.get("preMarketPrice") or q.get("price")); prior=number(q.get("previousClose")); return (price/prior-1)*100 if price and prior else None
    spy_gap=gap("SPY"); output=[]
    for row in rows:
        symbol=row["symbol"]; stock_gap=gap(symbol); sector_symbol=SECTOR_ETFS.get(row.get("sector")); sector_gap=gap(sector_symbol) if sector_symbol else None
        pre_spy=stock_gap-spy_gap if stock_gap is not None and spy_gap is not None else None; pre_sector=stock_gap-sector_gap if stock_gap is not None and sector_gap is not None else None
        stock_delta=pre_spy-row["nightly_rs_vs_spy_pct"] if pre_spy is not None and row.get("nightly_rs_vs_spy_pct") is not None else None
        sector_delta=pre_sector-row["nightly_sector_alpha_pct"] if pre_sector is not None and row.get("nightly_sector_alpha_pct") is not None else None
        high=(row.get("nightly_rs_vs_spy_pct") or 0)>0 and (row.get("nightly_sector_alpha_pct") or 0)>0
        ev=events.get(symbol,[]); polarities={e["polarity"] for e in ev}
        complete=all(v.get("complete_for_no-event_claim") for v in event_status.values())
        interaction="FRESH_POSITIVE_EVENT" if "POSITIVE" in polarities and "NEGATIVE" not in polarities else "FRESH_NEGATIVE_EVENT" if "NEGATIVE" in polarities and "POSITIVE" not in polarities else "NO_NEW_COMPANY_EVENT" if not ev and complete else "UNKNOWN"
        output.append({**row,"observed_at":utc_now().isoformat(),"premarket_gap_pct":stock_gap,"premarket_relative_to_spy_pct":pre_spy,"premarket_relative_to_sector_pct":pre_sector,
                       "change_stock_vs_spy_pct":stock_delta,"change_stock_vs_sector_pct":sector_delta,"spy_premarket_pct":spy_gap,"qqq_premarket_pct":gap("QQQ"),
                       "sector_etf":sector_symbol,"sector_premarket_pct":sector_gap,"market_sector_regime_change":"UNKNOWN_NOT_COMPARABLE_WITH_RETAINED_NIGHTLY_FIELDS",
                       "fresh_company_news":any(e["type"]=="news" for e in ev),"fresh_analyst_event":any(e["type"]=="analyst" for e in ev),
                       "fresh_earnings_event":any(e["type"]=="earnings" for e in ev),"fresh_guidance_event":any(e["guidance_flag"] for e in ev),
                       "events":ev,"information_interaction":interaction,"acceleration_state":classify(high,stock_delta,sector_delta),
                       "premarket_quality":"SOURCE_ERROR" if source_error else "FRESH","premarket_missing_reason":source_error})
    path=write_immutable(source.parent/"momentum_acceleration_0915.json",{"schema_version":1,"research_only":True,"non_trading":True,"created_at":utc_now().isoformat(),"nightly_artifact":str(source),"quote_source_error":source_error,"event_source_status":event_status,"rows":output})
    return {"ok":True,"path":str(path),"rows":len(output),"source_error":source_error,"state_counts":{s:sum(r["acceleration_state"]==s for r in output) for s in STATES}}


def update() -> dict[str,Any]:
    artifacts=sorted(RESEARCH_ROOT.glob("*/*/momentum_acceleration_0915.json")); rows=[]
    for path in artifacts:
        for row in (read_json(path,{}) or {}).get("rows",[]):rows.append((path,row))
    symbols={r["symbol"] for _,r in rows}|{"SPY"}|set(SECTOR_ETFS.values()); mark=capture_daily_marks(symbols); series=load_price_series(symbols)
    labelled=[{**row,"acceleration_artifact":str(path),**label(row,series)} for path,row in rows]; now=utc_now(); stamp=now.strftime("%Y%m%dT%H%M%SZ"); directory=RESEARCH_ROOT/"scoreboards"
    outcomes=write_immutable(directory/f"momentum_acceleration_outcomes_{stamp}.json",{"schema_version":1,"research_only":True,"non_trading":True,"created_at":now.isoformat(),"rows":labelled})
    report=[]
    for state in STATES:
        for interaction in INTERACTIONS:
            group=[r for r in labelled if r.get("acceleration_state")==state and r.get("information_interaction")==interaction]
            if not group:continue
            item={"acceleration_state":state,"information_interaction":interaction,"n":len(group),"unique_dates":len({r['trading_date'] for r in group}),"missing_pct":100*sum(r.get("outcome_state")=="MISSING_PRICE" for r in group)/len(group)}
            for horizon in (1,2,3,5,7):
                vals=[r[f"d{horizon}"]["spy_adjusted_return_pct"] for r in group if (r.get(f"d{horizon}") or {}).get("state")=="AVAILABLE" and r[f"d{horizon}"].get("spy_adjusted_return_pct") is not None]
                item[f"mean_spy_adjusted_d{horizon}"]=statistics.mean(vals) if vals else None; item[f"median_spy_adjusted_d{horizon}"]=statistics.median(vals) if vals else None
            item["evidence_state"]="PRELIMINARY_CHECK_ONLY" if item["unique_dates"]>=30 else "TOO_THIN_NOT_A_VERDICT"; report.append(item)
    scoreboard=write_immutable(directory/f"momentum_acceleration_scoreboard_{stamp}.json",{"schema_version":1,"research_only":True,"non_trading":True,"created_at":now.isoformat(),"minimum_dates":30,"preferred_dates":60,"groups":report})
    return {"ok":True,"artifacts":len(artifacts),"rows":len(labelled),"daily_mark":mark,"outcomes":str(outcomes),"scoreboard":str(scoreboard)}


def main()->None:
    parser=argparse.ArgumentParser(); parser.add_argument("command",choices=("create-nightly","capture-0915","update","self-test")); args=parser.parse_args()
    result=create_nightly() if args.command=="create-nightly" else capture_premarket() if args.command=="capture-0915" else update() if args.command=="update" else {"ok":True,"states":STATES,"interactions":INTERACTIONS,"broker_modules_imported":False}
    print(json.dumps(result,indent=2))


if __name__=="__main__":main()
