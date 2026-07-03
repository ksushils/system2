#!/usr/bin/env python3
"""Quick test of pipeline failure handling without running the full pipeline."""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_LOG_DIR = ROOT / "logs"
RUN_LOG_DIR.mkdir(exist_ok=True)


def run_step(name, args, env):
    import subprocess
    started = time.time()
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return {
        "name": name,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "runtimeSeconds": round(time.time() - started, 2),
        "stdoutTail": proc.stdout[-4000:],
        "stderrTail": proc.stderr[-4000:],
    }


def main():
    env = os.environ.copy()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    env["SYSTEM2_RUN_ID"] = run_id
    run_date = datetime.now(timezone.utc).date().isoformat()

    steps = [
        ("Pass step", ["-c", "print('ok')"]),
        ("Fail step", ["-c", "import nonexistent_module"]),
        ("Never runs", ["-c", "print('should not see this')"]),
    ]

    results = []
    failed_step = None
    for name, args in steps:
        result = run_step(name, args, env)
        results.append(result)
        if not result["ok"]:
            failed_step = result
            break

    all_ok = all(r["ok"] for r in results) and len(results) == len(steps)
    status = "SUCCESS" if all_ok else "FAILED"

    summary = {
        "run_id": run_id,
        "pipeline_status": status,
        "ok": all_ok,
        "steps": results,
    }

    if failed_step:
        summary["failed_at_step"] = failed_step["name"]
        summary["failure_reason"] = failed_step.get("stderrTail", "")[:500]
        summary["telegram_alert"] = {
            "simulated": True,
            "message": f"PIPELINE FAILED {run_date} at {failed_step['name']}"
        }

    log_path = RUN_LOG_DIR / f"test_failure_{run_id}.json"
    log_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))

    # Assertions for validation
    assert not all_ok, "Expected failure"
    assert failed_step["name"] == "Fail step", f"Expected fail at Fail step, got {failed_step['name']}"
    assert len(results) == 2, f"Expected 2 results, got {len(results)}"
    assert summary["pipeline_status"] == "FAILED"
    assert "telegram_alert" in summary
    print("\n✓ All assertions passed — failure handling works correctly.")


if __name__ == "__main__":
    main()
