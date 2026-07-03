#!/usr/bin/env python3
"""Minimal auth smoke test for the Stage 6 council models."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def test_claude(key: str) -> tuple[bool, str]:
    try:
        url = "https://api.anthropic.com/v1/messages"
        data = json.dumps({"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 10}).encode()
        req = urllib.request.Request(url, data=data, headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
        text = resp.get("content", [{}])[0].get("text", "")
        return text.strip().upper().startswith("OK"), text[:50]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def test_openai(key: str) -> tuple[bool, str]:
    try:
        url = "https://api.openai.com/v1/chat/completions"
        data = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 10}).encode()
        req = urllib.request.Request(url, data=data, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
        text = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        return text.strip().upper().startswith("OK"), text[:50]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def test_gemini(key: str) -> tuple[bool, str]:
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
        data = json.dumps({"contents": [{"parts": [{"text": "Say OK"}]}]}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
        text = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        return text.strip().upper().startswith("OK"), text[:50]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def test_kimi(key: str) -> tuple[bool, str]:
    try:
        url = "https://api.moonshot.cn/v1/chat/completions"
        data = json.dumps({"model": "moonshot-v1-8k", "messages": [{"role": "user", "content": "Say OK"}]}).encode()
        req = urllib.request.Request(url, data=data, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
        text = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        return text.strip().upper().startswith("OK"), text[:50]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


MODELS = {
    "claude": ("ANTHROPIC_API_KEY", test_claude),
    "gpt4o": ("OPENAI_API_KEY", test_openai),
    "gemini": ("GEMINI_API_KEY", test_gemini),
    "kimi": ("KIMI_API_KEY", test_kimi),
}


def main() -> int:
    load_dotenv()
    results: dict[str, dict] = {}
    ok_count = 0
    for name, (env_key, tester) in MODELS.items():
        key = os.environ.get(env_key, "").strip()
        if not key:
            results[name] = {"ok": False, "detail": f"{env_key} not set"}
            print(f"{name:10s} FAIL — {env_key} not set")
            continue
        ok, detail = tester(key)
        results[name] = {"ok": ok, "detail": detail}
        status = "OK  " if ok else "FAIL"
        print(f"{name:10s} {status} — {detail}")
        if ok:
            ok_count += 1

    summary = {"ok_count": ok_count, "total": len(MODELS), "models": results}
    print(f"\nCouncil auth: {ok_count}/{len(MODELS)} models authenticated")
    out = ROOT / "logs" / "council_auth_test.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if ok_count == len(MODELS) else 1


if __name__ == "__main__":
    sys.exit(main())
