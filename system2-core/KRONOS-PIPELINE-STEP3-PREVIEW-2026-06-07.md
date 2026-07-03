# Kronos Pipeline Step 3 Preview

Date: 2026-06-07

## Safety

- Paper mode only
- Manual preview root: `/root/system2-step3-preview`
- Live cron still targets `/root/system2-core/run_phase_b_core_baseline.sh`
- Manual-only regime override was used because the market was `RISK_OFF`
- Live runner and cron were not activated with Step 3

## Full Run

- Total stage runtime: 875.67 seconds (14m 35.67s)
- Universe: 800
- Stage 1 survivors: 646
- Stage 2 top set: 40
- News-safe forecast inputs: 40
- Clustered finalists: 16
- Paper ideas posted: 16
- Post errors: 0

Forecast timing:

- `chronos_inference_seconds`: 13.552
- `kronos_inference_seconds`: 210.861
- `total_forecast_seconds`: 224.413

Every Stage 2 top-40 record carried exactly 60 stored daily OHLCV bars. The
same bars were used for ATR and both forecast services.

## Five Finalists

| Ticker | Chronos | Chronos band | Kronos | Kronos band | Combined | Agree | Band | Forecast bonus |
|---|---|---:|---|---:|---|---|---:|---:|
| SYY | DOWN | 10.6196 | DOWN | 3.2761 | STRONG_DOWN | true | 6.9478 | 0 |
| FWONK | UP | 9.2299 | FLAT | 7.6988 | LEAN_UP | false | 8.4644 | 0 |
| MAR | FLAT | 11.0311 | DOWN | 6.5119 | LEAN_DOWN | false | 8.7715 | 0 |
| COO | UP | 11.5882 | UP | 9.8600 | STRONG_UP | true | 10.7241 | 0 |
| AER | UP | 10.0782 | FLAT | 7.8312 | LEAN_UP | false | 8.9547 | 0 |

There were 33 combined bands above 5%. All 33 received zero forecast
confluence contribution.

Tight examples:

- KVUE: agreed `STRONG_DOWN`, 3.1931% band, `-2`
- AIG: `CONFLICTED`, 4.5916% band, `0`
- YUM: `LEAN_UP`, 4.5502% band, `+2`

## Persistence

The `/api/idea` schema was extended to store:

- Chronos direction, band, and conviction
- Kronos direction, band, conviction, and 1d/3d/5d forecasts
- Combined direction, agreement, combined band
- Forecast confluence contribution

The run-metadata schema now stores all three forecast timing values.

## Fail-Open Tests

- Kronos unavailable: 40/40 continued using Chronos-only direction
- Both unavailable: 40/40 continued with `NO_FORECAST`
- Neither failure blocked the pipeline

