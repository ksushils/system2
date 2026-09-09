#!/usr/bin/env python3
"""Detect likely plaintext credentials without ever printing their values."""

import argparse
import json
import re
import subprocess
from pathlib import Path

DEFAULT_ROOTS = (
    Path("/root/fund-system/scanners-v7.1-revision/workflows"),
    Path("/root/fund-system/workflows-deploy"),
)
MAX_BYTES = 50_000_000
TEXT_SUFFIXES = {".json", ".js", ".cjs", ".mjs", ".py", ".sh", ".env", ".yaml", ".yml", ".toml", ".txt"}

RULES = {
    "TELEGRAM_BOT_TOKEN": re.compile(r"\bTELEGRAM_BOT_TOKEN\b\s*[:=]\s*['\"]?(?!\s*(?:REDACTED|PLACEHOLDER|YOUR_|\$\{|process\.env))[0-9]{6,12}:[A-Za-z0-9_-]{20,}", re.I),
    "PRIVATE_KEY": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "PASSWORD_FIELD": re.compile(r"\b(?:password|passwd|passphrase)\b\s*[:=]\s*['\"](?!\s*(?:REDACTED|PLACEHOLDER|YOUR_|\$\{|process\.env))[^'\"\r\n]{6,}['\"]", re.I),
    "SECRET_FIELD": re.compile(r"\b(?:api[_-]?secret|client[_-]?secret|alpaca_secret)\b\s*[:=]\s*['\"](?!\s*(?:REDACTED|PLACEHOLDER|YOUR_|\$\{|process\.env))[^'\"\r\n]{12,}['\"]", re.I),
    "API_KEY_FIELD": re.compile(r"\b(?:api[_-]?key|fmp_api_key|gemini_api_key|alpaca_key|cap_api_key)\b\s*[:=]\s*['\"](?!\s*(?:REDACTED|PLACEHOLDER|YOUR_|\$\{|process\.env))[^'\"\r\n]{12,}['\"]", re.I),
    "TOKEN_FIELD": re.compile(r"\b(?:access[_-]?token|bot[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"](?!\s*(?:REDACTED|PLACEHOLDER|YOUR_|\$\{|process\.env))[^'\"\r\n]{12,}['\"]", re.I),
}


def staged_files(repo: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True,
    )
    return [repo / line for line in result.stdout.splitlines() if line.strip()]


def candidates(roots: list[Path]):
    for root in roots:
        if root.is_file():
            yield root
        elif root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                    yield path


def scan(paths: list[Path]):
    findings = []
    scanned = 0
    for path in candidates(paths):
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        scanned += 1
        kinds = sorted(name for name, rule in RULES.items() if rule.search(text))
        if kinds:
            findings.append({"path": str(path), "classifications": kinds})
    return scanned, findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--staged", type=Path, metavar="REPO")
    parser.add_argument("--max-findings", type=int, default=200)
    args = parser.parse_args()
    paths = staged_files(args.staged.resolve()) if args.staged else (args.paths or list(DEFAULT_ROOTS))
    scanned, findings = scan(paths)
    result = {
        "ok": not findings,
        "status": "OK" if not findings else "CREDENTIAL_EXPOSURE_DETECTED",
        "files_scanned": scanned,
        "finding_count": len(findings),
        "findings": findings[: max(0, args.max_findings)],
        "findings_truncated": max(0, len(findings) - max(0, args.max_findings)),
        "secret_values_printed": False,
    }
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if not findings else 2)


if __name__ == "__main__":
    main()
