#!/usr/bin/env python3
"""Read-only crontab drift checker.

Compares required job signatures from canonical_crontab.txt against live crontab.
Detection and alert only: this script never modifies cron.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/root/system2-core')
CANONICAL = ROOT / 'canonical_crontab.txt'
ALERT = ROOT / 'logs' / 'crontab_drift_alert.txt'
LOG = ROOT / 'logs' / 'crontab_drift_check.log'

REQUIRED_SIGNATURES = [
    {'name': 'nightly pipeline', 'tokens': ['run_phase_b_core_baseline.sh']},
    {'name': 'PMF gap check', 'tokens': ['pre-market-gap/run-local'], 'exclude': ['cohort']},
    {'name': 'open confirmation', 'tokens': ['run_open_confirmation.sh']},
    {'name': 'PMF_LATE check', 'tokens': ['pre-market-gap/run-local', 'cohort', 'late']},
    {'name': 'PEAD tracker', 'tokens': ['pead_drift_tracker.py', '--write', '--lookback-days', '14']},
    {'name': 'auto-exec exit sync', 'tokens': ['pmf-auto-exec/sync-local']},
    {'name': 'validated backup', 'tokens': ['validated_fund_backup.py']},
    {'name': 'intraday recorder', 'tokens': ['intraday_bar_recorder.py', '--window-minutes', '120']},
    {'name': 'crontab drift checker', 'tokens': ['check_crontab.py']},
]


def normalize(line: str) -> str:
    return ' '.join(line.strip().split())


def active_lines_from_text(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith('#') or s.startswith('CRON_TZ='):
            continue
        out.append(normalize(raw))
    return out


def live_crontab() -> str:
    return subprocess.check_output(['crontab', '-l'], text=True)


def present(signature: dict[str, Any], lines: list[str]) -> bool:
    tokens = signature.get('tokens', [])
    excludes = signature.get('exclude', [])
    for line in lines:
        if all(token in line for token in tokens) and not any(token in line for token in excludes):
            return True
    return False


def check_text(live_text: str, canonical_text: str) -> dict[str, Any]:
    live_lines = active_lines_from_text(live_text)
    canonical_lines = active_lines_from_text(canonical_text)
    required = []
    for sig in REQUIRED_SIGNATURES:
        in_canonical = present(sig, canonical_lines)
        in_live = present(sig, live_lines)
        required.append({'name': sig['name'], 'in_canonical': in_canonical, 'present': in_live, 'tokens': sig['tokens']})
    missing = [row for row in required if row['in_canonical'] and not row['present']]
    missing_from_canonical = [row for row in required if not row['in_canonical']]
    return {
        'ok': not missing and not missing_from_canonical,
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'required': required,
        'missing': missing,
        'missing_from_canonical': missing_from_canonical,
        'live_job_count': len(live_lines),
        'canonical_job_count': len(canonical_lines),
    }


def write_log(result: dict[str, Any]) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(result, sort_keys=True) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-file', help='simulate live crontab from file instead of crontab -l')
    parser.add_argument('--canonical', default=str(CANONICAL))
    args = parser.parse_args()
    canonical_path = Path(args.canonical)
    if not canonical_path.exists():
        raise SystemExit(f'canonical crontab missing: {canonical_path}')
    canonical_text = canonical_path.read_text(encoding='utf-8')
    live_text = Path(args.live_file).read_text(encoding='utf-8') if args.live_file else live_crontab()
    result = check_text(live_text, canonical_text)
    write_log(result)
    if result['ok']:
        if ALERT.exists():
            ALERT.unlink()
        print(json.dumps({'ok': True, 'message': 'all required cron jobs present', 'live_job_count': result['live_job_count']}, indent=2))
        return 0
    ALERT.parent.mkdir(parents=True, exist_ok=True)
    ALERT.write_text(json.dumps(result, indent=2, sort_keys=True), encoding='utf-8')
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
