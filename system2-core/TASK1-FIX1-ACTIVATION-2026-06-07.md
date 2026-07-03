# Task 1 Fix 1 Activation

Activated in paper mode on 2026-06-07.

## Backups

- `/root/system2-core/backups/fix1-20260607T190229Z`
- `/root/fund-system/public/system2-terminal.html.bak-fix1-20260607T190229Z`
- `/root/fund-system/server/scoring-endpoints.cjs.bak-fix1-20260607T190229Z`

## Live Order

1. Universe
2. Stage 1 liquidity
3. Stage 2 technical
4. Stage 3 News Kill-Filter
5. Stage 4 Options Enrichment
6. Stage 5 Chronos + Kronos Forecasts
7. Stage 6 Council of AIs
8. Stage 7 Correlation/Cluster
9. Finalists

Authenticated `GET /api/run-metadata` returned counts:
`800 -> 646 -> 40 -> 39 -> 39 -> 39 -> 39 -> 15 -> 15`.

Cron remains:
`15 2 * * 2-6 /root/system2-core/run_phase_b_core_baseline.sh`

The active source and dashboard contain no remaining references to
`Stage 5 News` or `Stage 3 Options`.
