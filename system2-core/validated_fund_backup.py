#!/usr/bin/env python3
"""Validated rotating backup for /root/fund-system/data/fund.json.

Pure safety infrastructure: copies and validates fund.json into a persistent
backup directory. Does not mutate fund.json or trading data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SRC = Path('/root/fund-system/data/fund.json')
BACKUP_ROOT = Path('/root/system2-core/backups/fund-validated')
DAILY_DIR = BACKUP_ROOT / 'daily'
WEEKLY_DIR = BACKUP_ROOT / 'weekly'
LOG_PATH = BACKUP_ROOT / 'backup.log'
MANIFEST_PATH = BACKUP_ROOT / 'manifest.jsonl'
ALERT_PATH = BACKUP_ROOT / 'BACKUP_ALERT.json'
EXPECTED_KEYS = ['fund', 'investors', 'trades', 'ideas', 'pead_drift_paper']
MIN_IDEAS_WITHOUT_PRIOR = 1
ROW_DROP_FRACTION = 0.10
KEEP_DAILY = 14
KEEP_WEEKLY = 4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def log(level: str, message: str, **extra: Any) -> None:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    row = {'at': utc_now().isoformat(), 'level': level, 'message': message, **extra}
    line = json.dumps(row, sort_keys=True)
    print(line)
    with LOG_PATH.open('a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def alert(message: str, **extra: Any) -> None:
    payload = {'at': utc_now().isoformat(), 'message': message, **extra}
    ALERT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')
    log('ERROR', message, **extra)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load_last_good() -> dict[str, Any] | None:
    if not MANIFEST_PATH.exists():
        return None
    last = None
    with MANIFEST_PATH.open('r', encoding='utf-8') as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get('ok') is True:
                last = row
    return last


def validate_backup(path: Path, prior: dict[str, Any] | None) -> dict[str, Any]:
    size = path.stat().st_size
    try:
        with path.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception as exc:
        raise ValueError(f'JSON parse failed: {exc}')
    if not isinstance(data, dict):
        raise ValueError('top-level JSON is not an object')
    missing = [k for k in EXPECTED_KEYS if k not in data]
    if missing:
        raise ValueError(f'missing expected top-level keys: {missing}')
    if not isinstance(data.get('ideas'), list):
        raise ValueError('ideas is not a list')
    if not isinstance(data.get('pead_drift_paper'), list):
        raise ValueError('pead_drift_paper is not a list')
    ideas_count = len(data.get('ideas') or [])
    pead_count = len(data.get('pead_drift_paper') or [])
    if prior and prior.get('ideas_count') is not None:
        prev = int(prior.get('ideas_count') or 0)
        min_allowed = max(MIN_IDEAS_WITHOUT_PRIOR, int(prev * (1 - ROW_DROP_FRACTION)))
        if ideas_count < min_allowed:
            raise ValueError(f'ideas row count dropped unexpectedly: {ideas_count} < sane minimum {min_allowed} from prior {prev}')
    elif ideas_count < MIN_IDEAS_WITHOUT_PRIOR:
        raise ValueError(f'ideas row count implausibly low: {ideas_count}')
    return {
        'size': size,
        'sha256': sha256(path),
        'ideas_count': ideas_count,
        'pead_drift_paper_count': pead_count,
        'top_level_keys': sorted(data.keys()),
    }


def rotate() -> None:
    daily = sorted(DAILY_DIR.glob('fund.*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in daily[KEEP_DAILY:]:
        p.unlink(missing_ok=True)
    weekly = sorted(WEEKLY_DIR.glob('fund.week*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in weekly[KEEP_WEEKLY:]:
        p.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default=str(SRC))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    src = Path(args.source)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        alert('fund.json source missing', source=str(src))
        return 2
    ts = utc_now().strftime('%Y%m%dT%H%M%SZ')
    final = DAILY_DIR / f'fund.{ts}.json'
    tmp = DAILY_DIR / f'.fund.{ts}.json.tmp'
    prior = load_last_good()
    try:
        shutil.copy2(src, tmp)
        meta = validate_backup(tmp, prior)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        alert('validated fund backup failed; invalid copy removed', source=str(src), error=str(exc), prior=prior)
        return 2
    if args.dry_run:
        tmp.unlink(missing_ok=True)
        log('INFO', 'validated fund backup dry-run ok', source=str(src), **meta)
        return 0
    tmp.replace(final)
    latest = BACKUP_ROOT / 'latest.valid.json'
    shutil.copy2(final, latest)
    week = utc_now().strftime('%G-W%V')
    weekly = WEEKLY_DIR / f'fund.week.{week}.{ts}.json'
    if not any(WEEKLY_DIR.glob(f'fund.week.{week}.*.json')):
        shutil.copy2(final, weekly)
    manifest = {
        'ok': True,
        'at': utc_now().isoformat(),
        'path': str(final),
        'latest': str(latest),
        'weekly_created': str(weekly) if weekly.exists() else None,
        'source': str(src),
        **meta,
    }
    with MANIFEST_PATH.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(manifest, sort_keys=True) + '\n')
    rotate()
    log('INFO', 'validated fund backup created', **manifest)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
