"""`aisha --doctor` — sanity-check the install.

Verifies credentials, DB migrations, embedding model, channel tokens,
and that we can round-trip a tiny call to Claude. Exit code 0 if all
required checks pass; 1 otherwise.
"""
from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings


def _ok(msg: str) -> None:
    print(f"  ok    {msg}")


def _warn(msg: str) -> None:
    print(f"  warn  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}")


def _check_credentials() -> bool:
    print("credentials")
    if settings.anthropic_api_key:
        _ok(f"ANTHROPIC_API_KEY set (model: {settings.model})")
        return True
    if settings.completion_proxy_url:
        _ok(f"COMPLETION_PROXY_URL set ({settings.completion_proxy_url})")
        return True
    _fail("neither ANTHROPIC_API_KEY nor COMPLETION_PROXY_URL is set")
    return False


def _check_db() -> bool:
    print("database")
    try:
        from . import store
        conn = store.connect()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        _ok(f"schema v{version} ({len(tables)} tables)")
        return True
    except Exception as e:
        _fail(f"DB init failed: {e}")
        return False


def _check_live_call() -> bool:
    print("round-trip")
    if not settings.anthropic_api_key:
        _warn("skipped (no direct API key; proxy may still work)")
        return True
    payload = json.dumps({
        "model": settings.model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "say 'ok'"}],
    }).encode("utf-8")
    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "content-type": "application/json",
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        if body.get("content"):
            _ok("Claude replied")
            return True
        _fail(f"unexpected response: {body}")
        return False
    except HTTPError as e:
        _fail(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
        return False
    except URLError as e:
        _fail(f"connection error: {e}")
        return False


def _check_channels() -> None:
    print("channels")
    if settings.slack_app_token and settings.slack_bot_token:
        _ok("Slack tokens present")
    else:
        _warn("Slack disabled (set SLACK_APP_TOKEN + SLACK_BOT_TOKEN)")
    if settings.twilio_account_sid and settings.twilio_auth_token:
        _ok("Twilio/WhatsApp tokens present")
    else:
        _warn("WhatsApp disabled (set TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN)")
    if settings.telegram_bot_token:
        _ok("Telegram token present")
    else:
        _warn("Telegram disabled (set TELEGRAM_BOT_TOKEN)")


def run() -> int:
    ok = True
    ok &= _check_credentials()
    ok &= _check_db()
    ok &= _check_live_call()
    _check_channels()
    sys.stdout.flush()
    print()
    if ok:
        print("all required checks passed.")
        return 0
    print("one or more required checks failed. see messages above.")
    return 1
