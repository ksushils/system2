#!/usr/bin/env python3
"""Offline fixtures for research measurement V2. No network or broker access."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from research_price_resolver import ResearchPriceResolver, validate_premarket_quote
import research_telemetry_common as common
from research_telemetry_common import independent_membership_rows, session_offset, session_record, version_resolved_outcomes
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
    fixture_row = {"cohort":"TEST","trading_date":"2026-09-08","symbol":"TST"}
    unique, duplicates = independent_membership_rows(
        [(Path("20260905-run/membership.json"), fixture_row), (Path("20260908-rerun/membership.json"), dict(fixture_row))],
        ("cohort", "trading_date", "symbol"),
    )
    assert len(unique) == 1 and len(duplicates) == 1

    with tempfile.TemporaryDirectory() as tmp:
        old_root = common.RESEARCH_ROOT
        common.RESEARCH_ROOT = Path(tmp)
        try:
            initial = {"cohort":"TEST","symbol":"TST","trading_date":"2026-09-08","d1":{"state":"AVAILABLE","target_market_date":"2026-09-09","close":101.0,"close_provenance":{"provider":"fixture","provider_timestamp":"2026-09-09T20:00:00Z","source_file":"A.json"},"raw_return_pct":1.0,"spy_return_pct":0.0,"spy_adjusted_return_pct":1.0,"sector_return_pct":0.0,"sector_adjusted_return_pct":1.0}}
            version_resolved_outcomes([initial], "fixture", "2026-09-09T21:00:00Z")
            revised = json.loads(json.dumps(initial))
            revised["d1"].update({"close":102.0,"raw_return_pct":2.0,"spy_adjusted_return_pct":2.0,"sector_adjusted_return_pct":2.0})
            revised["d1"]["close_provenance"]["source_file"] = "B.json"
            second, stats = version_resolved_outcomes([revised], "fixture", "2026-09-10T21:00:00Z")
            versions = sorted(Path(tmp).glob("outcome_versions_v2/fixture/*/*/v*.json"))
            assert len(versions) == 2
            assert json.loads(versions[0].read_text())["price_value"] == 101.0
            assert json.loads(versions[1].read_text())["price_value"] == 102.0
            assert second[0]["d1"]["revision_state"] == "UPSTREAM_PRICE_REVISION"
            assert stats["upstream_price_revisions"] == 1
        finally:
            common.RESEARCH_ROOT = old_root
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
