# Task 1 Fix 2 Activation

Paper mode only. Activated 2026-06-07.

## Decision

The Alpaca 1-minute comparison was material:

- 19 scored ideas compared
- 4 ideas (21.1%) changed by at least 0.5%
- Maximum MFE difference: 1.554%
- Maximum MAE difference: 1.432%
- One stop/target status changed

Fix 2 was therefore activated with Alpaca wired, not as an FMP-only scorer.
FMP remains the fail-open daily-bar fallback.

## Live Changes

- Immutable actual entry from the first market open
- Alpaca 1-minute MFE/MAE and stop/target ordering
- SPY return over the resolved trade interval
- Actual R as the primary Phase D metric
- Planned R retained for legacy records
- Scoreboard entry, gap, R, MFE/MAE, and SPY columns
- Average entry gap headline statistic
- Pending and legacy labels

## Operations

- Backup: `/root/system2-core/backups/fix2-20260607T212846Z`
- n8n workflow: `System 2 — Daily Scorer` (`JbUOYzfnzCFuSRZP`)
- Active schedule: `0 23 * * 1-5` UTC
- Next run: Monday, 2026-06-08 at 23:00 UTC
- Dashboard and scorer checks passed.
- No live trading or broker execution was enabled.
