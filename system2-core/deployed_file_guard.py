#!/usr/bin/env python3
"""Alert when deployed safety-critical files differ from GitHub master."""

import argparse
import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/system2-core")
STATE = ROOT / "logs/deployed_file_guard_state.json"
FILES = {
    "fund-system/server/scoring-endpoints.cjs": Path("/root/fund-system/server/scoring-endpoints.cjs"),
    "fund-system/server/pmf-auto-executor.cjs": Path("/root/fund-system/server/pmf-auto-executor.cjs"),
}
RAW_BASE = "https://raw.githubusercontent.com/ksushils/system2/master"


def load_env():
    for line in (ROOT / ".env").read_text(errors="ignore").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def send_alert(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return {"sent": False, "reason": "missing Telegram credentials"}
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(request, timeout=30) as response:
        return {"sent": response.status == 200}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    load_env()
    if args.self_test:
        delivery = send_alert("[TEST] DEPLOYED FILE DIFFERS FROM CANONICAL: simulated mismatch")
        print(json.dumps({"ok": delivery.get("sent") is True, "self_test": True, "telegram": delivery}))
        return
    mismatches = []
    hashes = {}
    for relative, deployed in FILES.items():
        local = deployed.read_bytes()
        with urllib.request.urlopen(f"{RAW_BASE}/{relative}", timeout=30) as response:
            canonical = response.read()
        hashes[relative] = {"deployed": sha256(local), "canonical": sha256(canonical)}
        if hashes[relative]["deployed"] != hashes[relative]["canonical"]:
            mismatches.append(relative)
    fingerprint = "|".join(mismatches)
    previous = json.loads(STATE.read_text()) if STATE.exists() else {}
    result = {"ok": not mismatches, "checked_at": datetime.now(timezone.utc).isoformat(), "mismatches": mismatches, "hashes": hashes}
    if mismatches and previous.get("fingerprint") != fingerprint:
        result["telegram"] = send_alert("DEPLOYED FILE DIFFERS FROM CANONICAL: " + ", ".join(mismatches))
    STATE.write_text(json.dumps({**result, "fingerprint": fingerprint}, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
