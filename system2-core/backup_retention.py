#!/usr/bin/env python3
"""Scoped retention for System2 backup SQLite files and rebuildable npm cache."""

import argparse
import json
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BACKUP_ROOTS = (Path("/root/backups"), Path("/root/fund-system/backups"))
PROTECTED_LABELS = ("release", "checkpoint", "critical", "forensic")
KEEP_DAILY = 7
KEEP_WEEKLY = 4
NPM_CACHE = Path("/root/.npm/_cacache")
NPM_CLEAN_THRESHOLD_PCT = 80


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_n8n_database(path: Path) -> bool:
    if path.suffix.lower() != ".sqlite" or not path.is_file():
        return False
    try:
        uri = f"file:{path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as db:
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('workflow_entity','execution_entity')"
            ).fetchall()
        return bool(rows)
    except sqlite3.Error:
        return False


def candidates() -> list[Path]:
    result = []
    for root in BACKUP_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.sqlite"):
            if is_within(path, root) and is_n8n_database(path):
                result.append(path)
    return sorted(result, key=lambda p: p.stat().st_mtime, reverse=True)


def retention_plan(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    protected = {p for p in paths if any(label in str(p).lower() for label in PROTECTED_LABELS)}
    unprotected = [p for p in paths if p not in protected]
    by_day: dict[str, list[Path]] = defaultdict(list)
    for path in unprotected:
        day = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date().isoformat()
        by_day[day].append(path)
    kept = set(protected)
    daily_days = sorted(by_day, reverse=True)[:KEEP_DAILY]
    for day in daily_days:
        kept.add(max(by_day[day], key=lambda p: p.stat().st_mtime))
    older = [p for p in unprotected if p not in kept]
    by_week: dict[str, list[Path]] = defaultdict(list)
    for path in older:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        iso = stamp.isocalendar()
        by_week[f"{iso.year}-W{iso.week:02d}"].append(path)
    for week in sorted(by_week, reverse=True)[:KEEP_WEEKLY]:
        kept.add(max(by_week[week], key=lambda p: p.stat().st_mtime))
    delete = [p for p in paths if p not in kept]
    return sorted(kept), sorted(delete)


def disk_used_pct() -> float:
    usage = shutil.disk_usage("/")
    return round(usage.used / usage.total * 100, 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply deletions; default is dry-run")
    args = parser.parse_args()
    found = candidates()
    keep, delete = retention_plan(found)
    deleted = []
    for path in delete:
        if args.apply:
            if not any(is_within(path, root) for root in BACKUP_ROOTS):
                raise RuntimeError(f"refusing unsafe path: {path}")
            size = path.stat().st_size
            path.unlink()
            deleted.append({"path": str(path), "bytes": size})
    npm_cleaned = False
    before_pct = disk_used_pct()
    if args.apply and before_pct >= NPM_CLEAN_THRESHOLD_PCT and NPM_CACHE.exists():
        subprocess.run(["npm", "cache", "clean", "--force"], check=True)
        npm_cleaned = True
    print(json.dumps({
        "ok": True,
        "dry_run": not args.apply,
        "policy": {"daily": KEEP_DAILY, "weekly": KEEP_WEEKLY, "protected_labels": PROTECTED_LABELS},
        "candidates": len(found),
        "keep": [str(p) for p in keep],
        "would_delete": [str(p) for p in delete],
        "deleted": deleted,
        "npm_clean_threshold_pct": NPM_CLEAN_THRESHOLD_PCT,
        "npm_cleaned": npm_cleaned,
        "disk_used_pct_before": before_pct,
        "disk_used_pct_after": disk_used_pct(),
    }, indent=2))


if __name__ == "__main__":
    main()
