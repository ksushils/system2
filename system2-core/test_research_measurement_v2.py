#!/usr/bin/env python3
"""Offline fixtures for research measurement V2. No network or broker access."""

from datetime import datetime, timezone

from research_price_resolver import ResearchPriceResolver, validate_premarket_quote
from research_telemetry_common import session_offset, session_record
from swing_shadow_cohorts import label


def run() -> None:
    resolver = ResearchPriceResolver(set())
    resolver.eod = {"TST":{"2026-09-03":{"open":100,"close":101,"_source_type":"CANONICAL_FMP_EOD","_source_file":"canonical.json","_provider":"fixture","_adjustment_basis":"UNKNOWN"}}}
    resolver.marks = {"TST":{"2026-09-03":[{"open":90,"close":91,"quality_state":"COMPLETED_SESSION","session_type":"COMPLETED_REGULAR","provider_timestamp":"2026-09-03T20:01:00Z","_source_file":"mark.json"}]}}
    assert resolver.resolve("TST","2026-09-03","SESSION_CLOSE")["price"] == 101, "batch mark overwrote canonical EOD"
    valid = validate_premarket_quote("TST", {"preMarketPrice":102,"previousClose":101,"price":999,"timestamp":int(datetime(2026,9,4,12,0,tzinfo=timezone.utc).timestamp())}, "2026-09-04")
    assert valid["quality_state"] == "PREMARKET_VALID" and valid["price"] == 102
    stale = validate_premarket_quote("TST", {"price":101,"previousClose":101,"timestamp":int(datetime(2026,9,4,12,0,tzinfo=timezone.utc).timestamp())}, "2026-09-04")
    assert stale["quality_state"] == "NO_PREMARKET_TRADE" and stale["price"] is None
    old = validate_premarket_quote("TST", {"preMarketPrice":102,"previousClose":101,"timestamp":int(datetime(2026,9,3,12,0,tzinfo=timezone.utc).timestamp())}, "2026-09-04")
    assert old["quality_state"] == "PREVIOUS_SESSION_STALE"
    # Common-anchor arithmetic: (110/100-1 - 105/100-1) - (108/100-1 - 104/100-1) = 1 percentage point.
    delta = (((110/100-1)-(105/100-1))-((108/100-1)-(104/100-1)))*100
    assert abs(delta-1.0) < 1e-10
    assert session_offset(datetime(2026,9,4).date(),1)["session_date"] == "2026-09-08"  # Labor Day Monday
    assert session_record(datetime(2026,11,27).date())["session_type"] == "HALF_DAY"
    assert session_offset(datetime(2026,12,31).date(),1)["session_date"] == "2027-01-04"  # cross-year New Year closure
    assert session_offset(datetime(2026,11,25).date(),1)["session_date"] == "2026-11-27"  # Thanksgiving
    class ExactDateFixture:
        def resolve(self, symbol, market_date, field_type):
            values = {("TST","2026-09-04","NEXT_OPEN"):100, ("TST","2026-09-08","SESSION_CLOSE"):102,
                      ("SPY","2026-09-04","NEXT_OPEN"):500}  # exact target SPY close intentionally absent
            value=values.get((symbol,market_date,field_type))
            return {"symbol":symbol,"market_date":market_date,"field_type":field_type,"price":value,"source_type":"FIXTURE","reason":None if value else "ABSENT"}
        def corporate_action_state(self, symbol, start_date, end_date):return {"state":"NO_RETAINED_ACTION_FOUND","actions":[]}
    labelled=label({"symbol":"TST","trading_date":"2026-09-04","next_open_timestamp":"2026-09-04T09:30:00-04:00","sector":None},ExactDateFixture())
    assert labelled["d1"]["target_market_date"]=="2026-09-08" and labelled["d1"]["state"]=="BENCHMARK_MISSING"
    class ActionFixture(ExactDateFixture):
        def corporate_action_state(self, symbol, start_date, end_date):return {"state":"CORPORATE_ACTION_UNRESOLVED","actions":[{"date":end_date}]}
    flagged=label({"symbol":"TST","trading_date":"2026-09-04","next_open_timestamp":"2026-09-04T09:30:00-04:00","sector":None},ActionFixture())
    assert flagged["d1"]["state"]=="CORPORATE_ACTION_UNRESOLVED"
    print("PASS research measurement V2 fixtures")


if __name__ == "__main__":
    run()
