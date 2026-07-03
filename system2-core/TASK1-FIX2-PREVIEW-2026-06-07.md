# Task 1 Fix 2 Preview and Activation

Paper mode. Activated in the live scorer on 2026-06-07 after preview approval.

## Implementation

- Alpaca daily bars are attempted first when credentials exist.
- FMP daily bars are the fail-open fallback.
- The original preview used FMP. Alpaca credentials were then installed and
  the same copied-store test was rerun using 1-minute bars.
- Actual entry is the first available market open on or after the idea date.
- Actual entry is immutable after it is populated.
- MFE/MAE use all daily highs/lows from entry through the scoring mark.
- Stop/target hits use intraday daily-bar lows/highs.
- SPY return uses SPY entry-day open through resolution-day close.
- Statistics use actual R first and planned R only for legacy records.

## Copied-Store Test

- Live-store snapshot ideas: 88
- Actual entry populated: 49
- Ideas with enough trading days to update: 19
- Pending or legacy: 39
- First run errors: 0
- Second run updates: 0
- Actual entries overwritten on second run: 0
- Average entry gap: -1.790%

June 4 ideas used the June 4 open. June 6 was a Saturday, so those ideas remain
`PENDING_MARKET_OPEN` until Monday, June 8, 2026 data is available. June 9,
2026 is Tuesday, not Monday.

## Sample

| Ticker | Idea date | Planned | Actual | Gap % | MFE % | MAE % | Actual R | Planned R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| NWSA | 2026-06-04 | 26.7058 | 26.42 | -1.070 | 3.255 | 0.530 | -1.000 | -1.000 |
| RTX | 2026-06-04 | 178.5328 | 176.54 | -1.116 | 3.393 | 0.198 | 6.370 | 0.913 |
| ES | 2026-06-04 | 69.5306 | 68.76 | -1.108 | 3.927 | 0.538 | -1.000 | -1.000 |
| VST | 2026-06-04 | 152.9116 | 151.54 | -0.897 | 1.815 | 2.666 | -1.000 | -1.000 |
| FROG | 2026-06-04 | 85.8088 | 82.90 | -3.390 | 4.258 | 2.292 | 0.000 | -2.249 |

The large RTX actual R is mathematically expected from the requested formula:
the favorable opening gap left a much smaller actual risk distance to the
unchanged planned stop.

Full report: `fix2-test-report.json`

## Alpaca Comparison

- Compared scored ideas: 19
- Material differences at or above 0.5%: 4 (21.1%)
- Mean absolute MFE difference: 0.270%
- Mean absolute MAE difference: 0.246%
- Maximum absolute MFE difference: 1.554%
- Maximum absolute MAE difference: 1.432%
- Hit-status changes: 1

The differences were material, so activation retained Alpaca 1-minute bars
with FMP as fail-open fallback. Full comparison:
`alpaca-vs-fmp-comparison.json`.

## Activation

- Backup: `/root/system2-core/backups/fix2-20260607T212846Z`
- Daily scorer workflow: `JbUOYzfnzCFuSRZP`
- Schedule: `0 23 * * 1-5` UTC, unchanged
- Next run: Monday, 2026-06-08 at 23:00 UTC
- Live trading remains disabled.
