#!/usr/bin/env python3
"""Shared helpers for additive, non-trading System2 research telemetry."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import exchange_calendars as xcals
except ImportError:  # Explicitly reported as PARTIAL; never silently authoritative.
    xcals = None

ROOT = Path(__file__).resolve().parent
RESEARCH_ROOT = ROOT / "data" / "research_telemetry"
NY = ZoneInfo("America/New_York")


def _xnys():
    return xcals.get_calendar("XNYS", start="2000-01-01", end="2035-12-31") if xcals is not None else None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _observed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    day = date(year, month, 1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    day = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    return day


def _easter(year: int) -> date:
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31; day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def market_holidays(year: int) -> set[date]:
    holidays = {
        _observed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_holiday(date(year, 6, 19)),
        _observed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_holiday(date(year, 12, 25)),
    }
    return holidays


def is_market_session(day: date) -> bool:
    if xcals is not None:
        return day.isoformat() in _xnys().schedule.index
    return day.weekday() < 5 and day not in market_holidays(day.year)


def session_record(day: date) -> dict[str, Any] | None:
    """Return authoritative XNYS session metadata when the maintained library is installed."""
    if xcals is None:
        if not is_market_session(day):
            return None
        opened = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
        thanksgiving = _nth_weekday(day.year, 11, 3, 4)
        early_close = day == thanksgiving + timedelta(days=1) or (day.month == 12 and day.day == 24 and day.weekday() < 5) or (day.month == 7 and day.day == 3 and day.weekday() < 5)
        closed = datetime(day.year, day.month, day.day, 13 if early_close else 16, 0, tzinfo=NY)
        return {"session_date": day.isoformat(), "open_timestamp_ET": opened.isoformat(), "close_timestamp_ET": closed.isoformat(), "session_type": "HALF_DAY" if early_close else "NORMAL", "calendar_source": "HANDMADE_FALLBACK_PARTIAL"}
    cal = _xnys()
    if day.isoformat() not in cal.schedule.index:
        return None
    schedule = cal.schedule.loc[day.isoformat()]
    opened = schedule["open"].to_pydatetime().astimezone(NY)
    closed = schedule["close"].to_pydatetime().astimezone(NY)
    normal_close = time(16, 0)
    session_type = "HALF_DAY" if closed.time() < normal_close else "NORMAL"
    return {"session_date": day.isoformat(), "open_timestamp_ET": opened.isoformat(), "close_timestamp_ET": closed.isoformat(), "session_type": session_type, "calendar_source": "exchange_calendars:XNYS"}


def session_offset(day: date, count: int) -> dict[str, Any] | None:
    if not is_market_session(day):
        return None
    current = day
    direction = 1 if count >= 0 else -1
    for _ in range(abs(count)):
        current += timedelta(days=direction)
        while not is_market_session(current):
            current += timedelta(days=direction)
    return session_record(current)


def next_market_session(after: datetime | None = None) -> dict[str, Any]:
    moment = (after or utc_now()).astimezone(NY)
    day = moment.date()
    record = session_record(day)
    open_time = datetime.fromisoformat(record["open_timestamp_ET"]) if record else datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
    if moment >= open_time or not is_market_session(day):
        day += timedelta(days=1)
        while not is_market_session(day):
            day += timedelta(days=1)
        record = session_record(day)
        open_time = datetime.fromisoformat(record["open_timestamp_ET"])
    prior = day - timedelta(days=1)
    weekend = prior.weekday() >= 5
    holiday = not weekend and not is_market_session(prior)
    return {
        "trading_session": day.isoformat(),
        "next_session_open": open_time.isoformat(),
        "hours_to_open": round((open_time.astimezone(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds() / 3600, 4),
        "weekend_carry": weekend,
        "holiday_carry": holiday,
        "session_date": day.isoformat(),
        "open_timestamp_ET": open_time.isoformat(),
        "close_timestamp_ET": record["close_timestamp_ET"] if record else None,
        "session_type": record["session_type"] if record else "SPECIAL",
        "calendar_source": record["calendar_source"] if record else "UNKNOWN",
    }


def run_id() -> str:
    return os.environ.get("SYSTEM2_RUN_ID") or utc_now().strftime("%Y%m%dT%H%M%SZ") + "-research"


def run_directory(session: str | None = None, identifier: str | None = None) -> Path:
    session = session or next_market_session()["trading_session"]
    path = RESEARCH_ROOT / session / (identifier or run_id())
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_authority(session: str) -> dict[str, Any] | None:
    """Latest immutable authority decision for an intended market session."""
    directory = RESEARCH_ROOT / "session_authority" / session
    decisions = sorted(directory.glob("*.json")) if directory.exists() else []
    return read_json(decisions[-1], {}) if decisions else None


def publish_session_authority(session: str, run: str, run_timestamp: str, source_artifact: Path) -> dict[str, Any]:
    """Append a supersession decision; never edit a previous run or membership."""
    previous = session_authority(session)
    if previous and previous.get("run_id") == run:
        return previous
    moment = utc_now()
    payload = {
        "schema_version": 1,
        "research_only": True,
        "run_id": run,
        "run_timestamp": run_timestamp,
        "intended_xnys_session": session,
        "authoritative_for_session": True,
        "supersedes_run_id": previous.get("run_id") if previous else None,
        "source_artifact": str(source_artifact),
        "decided_at": moment.isoformat(),
    }
    directory = RESEARCH_ROOT / "session_authority" / session
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{moment.strftime('%Y%m%dT%H%M%S%fZ')}_{_safe_component(run)}.json"
    write_immutable(path, payload)
    return {**payload, "authority_artifact": str(path)}


def authority_fields(session: str, fallback_run: str, fallback_timestamp: str) -> dict[str, Any]:
    decision = session_authority(session)
    return {
        "run_id": decision.get("run_id") if decision else fallback_run,
        "run_timestamp": decision.get("run_timestamp") if decision else fallback_timestamp,
        "intended_xnys_session": session,
        "authoritative_for_session": True,
        "supersedes_run_id": decision.get("supersedes_run_id") if decision else None,
    }


def experiment_authority(experiment: str, session: str) -> dict[str, Any] | None:
    """The sole authority decision for one experiment and intended XNYS session."""
    directory = RESEARCH_ROOT / "experiment_authority" / _safe_component(experiment) / session
    decisions = sorted(directory.glob("*.json")) if directory.exists() else []
    return read_json(decisions[-1], {}) if decisions else None


def publish_experiment_authority(
    experiment: str, session: str, run: str, run_timestamp: str, source_artifact: Path,
    *, status: str = "VALID", reason: str | None = None, metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append an explicit experiment/session authority decision; never alter membership."""
    previous = experiment_authority(experiment, session)
    if (previous and previous.get("authoritative_run_id") == run
            and previous.get("status") == status and previous.get("reason") == reason):
        return previous
    moment = utc_now()
    previous_run = previous.get("authoritative_run_id") if previous else None
    payload = {
        "schema_version": 1, "research_only": True, "non_trading": True,
        "experiment_name": experiment, "intended_xnys_session": session,
        "authoritative_run_id": run, "run_id": run, "run_timestamp": run_timestamp,
        "authoritative_for_session": True, "status": status, "reason": reason,
        "previous_run_id": previous_run, "new_run_id": run,
        "supersedes_run_id": previous_run,
        "superseded_at": moment.isoformat() if previous_run and previous_run != run else None,
        "source_artifact": str(source_artifact), "decided_at": moment.isoformat(),
        "metadata": metadata or {},
    }
    directory = RESEARCH_ROOT / "experiment_authority" / _safe_component(experiment) / session
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{moment.strftime('%Y%m%dT%H%M%S%fZ')}_{_safe_component(run)}.json"
    write_immutable(path, payload)
    return {**payload, "authority_artifact": str(path)}


def experiment_authority_fields(experiment: str, session: str, fallback_run: str, fallback_timestamp: str) -> dict[str, Any]:
    """Resolve only the experiment-scoped authority; never consult global session authority."""
    decision = experiment_authority(experiment, session)
    return {
        "run_id": decision.get("authoritative_run_id") if decision else fallback_run,
        "run_timestamp": decision.get("run_timestamp") if decision else fallback_timestamp,
        "intended_xnys_session": session,
        "authoritative_for_session": True,
        "supersedes_run_id": decision.get("supersedes_run_id") if decision else None,
    }


def authoritative_experiment_membership_rows(
    rows: list[tuple[Path, dict[str, Any]]], identity_fields: tuple[str, ...]
) -> tuple[list[tuple[Path, dict[str, Any]]], list[dict[str, Any]]]:
    """Evaluate only the experiment/session authority and retain terminal exclusion reasons."""
    selected: dict[tuple[Any, ...], tuple[Path, dict[str, Any]]] = {}
    terminal: list[dict[str, Any]] = []
    for path, row in sorted(rows, key=lambda item: str(item[0])):
        experiment = str(row.get("experiment_name") or "")
        session = str(row.get("intended_xnys_session") or row.get("trading_date") or "")
        identity = {field: row.get(field) for field in identity_fields}
        authority = experiment_authority(experiment, session) if experiment and session else None
        if not authority:
            terminal.append({"identity": identity, "membership_artifact": str(path), "classification": "AUTHORITY_UNKNOWN"})
            continue
        if authority.get("status") != "VALID":
            terminal.append({"identity": identity, "membership_artifact": str(path), "classification": "INVALID_SESSION_MEMBERSHIP",
                             "authority_status": authority.get("status"), "reason": authority.get("reason"),
                             "authoritative_run_id": authority.get("authoritative_run_id")})
            continue
        row_run = row.get("run_id") or row.get("pipeline_run_id")
        if row_run != authority.get("authoritative_run_id"):
            terminal.append({"identity": identity, "membership_artifact": str(path), "classification": "SUPERSEDED_RUN",
                             "authoritative_run_id": authority.get("authoritative_run_id")})
            continue
        key = tuple(row.get(field) for field in identity_fields)
        if key in selected:
            terminal.append({"identity": identity, "membership_artifact": str(path), "classification": "DUPLICATE_INTENDED_SESSION_MEMBERSHIP",
                             "kept_artifact": str(selected[key][0])})
            continue
        selected[key] = (path, row)
    return list(selected.values()), terminal


def content_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def _safe_component(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "UNKNOWN"))[:80]


def independent_membership_rows(
    rows: list[tuple[Path, dict[str, Any]]], identity_fields: tuple[str, ...]
) -> tuple[list[tuple[Path, dict[str, Any]]], list[dict[str, Any]]]:
    """Keep one research member per intended-session identity.

    Operational reruns remain on disk. When an explicit immutable session
    authority exists, evaluation uses only that run; legacy sessions without
    an authority decision retain the earliest-membership rule.
    """
    selected: dict[tuple[Any, ...], tuple[Path, dict[str, Any]]] = {}
    duplicates: list[dict[str, Any]] = []
    for path, row in sorted(rows, key=lambda item: str(item[0])):
        session = str(row.get("intended_xnys_session") or row.get("trading_date") or "")
        authority = session_authority(session) if session else None
        row_run = row.get("run_id") or row.get("pipeline_run_id")
        if authority and row_run != authority.get("run_id"):
            duplicates.append({
                "identity": {field: row.get(field) for field in identity_fields},
                "duplicate_artifact": str(path),
                "authoritative_run_id": authority.get("run_id"),
                "classification": "SUPERSEDED_INTENDED_SESSION_RUN",
            })
            continue
        identity = tuple(row.get(field) for field in identity_fields)
        if identity in selected:
            duplicates.append({
                "identity": dict(zip(identity_fields, identity)),
                "kept_artifact": str(selected[identity][0]),
                "duplicate_artifact": str(path),
                "classification": "DUPLICATE_INTENDED_SESSION_MEMBERSHIP",
            })
            continue
        selected[identity] = (path, row)
    return list(selected.values()), duplicates


def version_resolved_outcomes(
    rows: list[dict[str, Any]], family: str, resolution_timestamp: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Attach append-only provenance versions to every resolved horizon.

    A provider revision creates a new immutable version. An identical rerun
    reuses the prior version and its original resolution timestamp.
    """
    root = RESEARCH_ROOT / "outcome_versions_v2" / _safe_component(family)
    counters = {"created": 0, "reused": 0, "upstream_price_revisions": 0}
    for row in rows:
        namespace = row.get("cohort") or row.get("challenger") or row.get("acceleration_state_v2") or "UNGROUPED"
        for horizon in (1, 2, 3, 5, 7):
            key = f"d{horizon}"
            outcome = row.get(key)
            if not isinstance(outcome, dict) or outcome.get("state") not in {"AVAILABLE", "BENCHMARK_MISSING"}:
                continue
            identity = {
                "family": family,
                "namespace": namespace,
                "symbol": row.get("symbol") or row.get("ticker"),
                "entry_session": row.get("trading_date"),
                "target_horizon": key,
                "target_xnys_date": outcome.get("target_market_date"),
            }
            price_record = {
                **identity,
                "price_value": outcome.get("close"),
                "provider": (outcome.get("close_provenance") or {}).get("provider"),
                "provider_timestamp": (outcome.get("close_provenance") or {}).get("provider_timestamp"),
                "source_artifact_cache": (outcome.get("close_provenance") or {}).get("source_file"),
                "state": outcome.get("state"),
                "raw_return_pct": outcome.get("raw_return_pct"),
                "spy_return_pct": outcome.get("spy_return_pct"),
                "spy_adjusted_return_pct": outcome.get("spy_adjusted_return_pct"),
                "sector_return_pct": outcome.get("sector_return_pct"),
                "sector_adjusted_return_pct": outcome.get("sector_adjusted_return_pct"),
            }
            # Cache refresh paths are provenance, not economic revisions. A new
            # version is created only when the published value/returns change.
            hash_record = {key: value for key, value in price_record.items() if key not in {"source_artifact_cache", "provider_timestamp"}}
            outcome_hash = content_hash(hash_record)
            identity_hash = content_hash(identity)
            directory = root / identity_hash[:2] / identity_hash
            existing = sorted(directory.glob("v*_*.json")) if directory.exists() else []
            latest = read_json(existing[-1], {}) if existing else {}
            if latest.get("outcome_hash") == outcome_hash:
                version = int(latest.get("outcome_version") or len(existing) or 1)
                revision_state = latest.get("revision_state") or "ORIGINAL_PUBLISHED_OUTCOME"
                resolved_at = latest.get("resolution_timestamp") or resolution_timestamp
                previous_hash = latest.get("previous_outcome_hash")
                counters["reused"] += 1
            else:
                version = len(existing) + 1
                previous_hash = latest.get("outcome_hash") if latest else None
                revision_state = "UPSTREAM_PRICE_REVISION" if latest else "ORIGINAL_PUBLISHED_OUTCOME"
                resolved_at = resolution_timestamp
                payload = {
                    "schema_version": 1,
                    "namespace": "outcome_versions_v2",
                    "research_only": True,
                    "non_trading": True,
                    **price_record,
                    "resolution_timestamp": resolved_at,
                    "outcome_version": version,
                    "outcome_hash": outcome_hash,
                    "previous_outcome_hash": previous_hash,
                    "revision_state": revision_state,
                }
                directory.mkdir(parents=True, exist_ok=True)
                version_path = directory / f"v{version:04d}_{outcome_hash[:16]}.json"
                try:
                    write_immutable(version_path, payload)
                    counters["created"] += 1
                    if revision_state == "UPSTREAM_PRICE_REVISION":
                        counters["upstream_price_revisions"] += 1
                except FileExistsError:
                    counters["reused"] += 1
            outcome.update({
                "outcome_version": version,
                "outcome_hash": outcome_hash,
                "resolution_timestamp": resolved_at,
                "revision_state": revision_state,
                "previous_outcome_hash": previous_hash,
            })
    return rows, counters


def write_immutable(path: Path, payload: dict[str, Any]) -> Path:
    body = dict(payload)
    body["artifact_hash"] = content_hash(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(body, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return default
