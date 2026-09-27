#!/usr/bin/env python3
"""Fail-closed retention for two derived cumulative scoreboard families.

This utility never traverses research telemetry generally.  It considers only
the explicitly listed derived report filenames, retains the newest complete
snapshots by their embedded ``created_at`` timestamp, and requires ``--execute``
before unlinking an explicit allowlist.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/root/system2-core")
SCOREBOARDS = ROOT / "data/research_telemetry/scoreboards"
REPORTS = ROOT / "reports"
LOCK = ROOT / "data/ops/derived_scoreboard_retention_v1.lock"
LEDGER = REPORTS / "derived_scoreboard_retention_v1.jsonl"
KEEP_COUNT = 3
FAMILIES = {
    "stage1_v2_outcomes": re.compile(r"^stage1_v2_outcomes_(\d{8}T\d{6}Z)\.json$"),
    "unchased_challenger_outcomes_v2": re.compile(
        r"^unchased_challenger_outcomes_v2_(\d{8}T\d{6}Z)\.json$"
    ),
}
EXPECTED = {"schema_version": 2, "namespace": "outcomes_v2", "research_only": True, "non_trading": True}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def complete(path: Path, family: str, match: re.Match[str]) -> dict[str, Any] | None:
    try:
        if path in _open_paths():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            return None
        if any(payload.get(key) != value for key, value in EXPECTED.items()):
            return None
        created = str(payload.get("created_at") or "")
        parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return {
            "path": str(path), "family": family, "run_id": match.group(1),
            "created_at": parsed.astimezone(timezone.utc).isoformat(),
            "size_bytes": path.stat().st_size, "sha256": sha256(path),
        }
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None


def _open_paths() -> set[Path]:
    """Best-effort open-file protection; failures protect nothing from deletion."""
    proc = Path("/proc")
    result: set[Path] = set()
    try:
        for fd in proc.glob("[0-9]*/fd/*"):
            try:
                target = Path(os.readlink(fd))
                if str(target).startswith(str(SCOREBOARDS) + "/"):
                    result.add(target)
            except OSError:
                continue
    except OSError:
        # Callers treat an unreadable /proc as no eligible files by rejecting
        # execution below through the explicit process-access check.
        return {SCOREBOARDS / "__proc_unavailable__"}
    return result


def plan() -> dict[str, Any]:
    if not SCOREBOARDS.is_dir():
        raise RuntimeError(f"scoreboard root missing: {SCOREBOARDS}")
    all_entries: list[dict[str, Any]] = []
    protected: list[str] = []
    for family, pattern in FAMILIES.items():
        discovered: list[dict[str, Any]] = []
        for path in SCOREBOARDS.iterdir():
            match = pattern.fullmatch(path.name)
            if not match or not path.is_file():
                continue
            row = complete(path, family, match)
            if row is None:
                protected.append(str(path))
            else:
                discovered.append(row)
        discovered.sort(key=lambda row: (row["created_at"], row["run_id"]), reverse=True)
        for index, row in enumerate(discovered):
            row["retention_decision"] = "RETAIN" if index < KEEP_COUNT else "DELETE_CANDIDATE"
            row["approved_for_delete"] = index >= KEEP_COUNT
            all_entries.append(row)
    return {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "keep_count": KEEP_COUNT,
        "approved_families": sorted(FAMILIES),
        "entries": all_entries,
        "protected_incomplete_or_open": protected,
    }


def execute(payload: dict[str, Any]) -> list[dict[str, Any]]:
    deleted: list[dict[str, Any]] = []
    for entry in payload["entries"]:
        if not entry["approved_for_delete"]:
            continue
        path = Path(entry["path"])
        if path.parent != SCOREBOARDS or not any(pattern.fullmatch(path.name) for pattern in FAMILIES.values()):
            raise RuntimeError(f"refusing path outside approved families: {path}")
        if not path.is_file() or path.stat().st_size != entry["size_bytes"] or sha256(path) != entry["sha256"]:
            raise RuntimeError(f"file changed since allowlist generation: {path}")
        path.unlink()
        deleted.append(entry)
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Delete only the generated explicit allowlist.")
    args = parser.parse_args()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("retention already running") from exc
        payload = plan()
        deleted = execute(payload) if args.execute else []
        payload.update({
            "dry_run": not args.execute,
            "deleted": deleted,
            "bytes_reclaimed": sum(row["size_bytes"] for row in deleted),
            "broker_calls": 0,
            "broker_orders": 0,
            "shadow_activation": False,
        })
        with LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
